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
and scopes `describe` lists, the first lines a probe watches for a call's start. What
the compiler makes of a source differs between Python versions and builds, so each is
read by the program's interpreter: in this process where it compiles as this one does
(python_passes.same_compiler), else in a helper process of that interpreter, which
imports this Peeky by absolute path (`-I`). No reading made on one version is applied to
another. So the interpreter must be CPython 3.11 or later, as Peeky is; another one is
refused before anything runs.
"""

import atexit
import json
import os
import select
import shutil
import subprocess
import sys
import threading
import time
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
IDENTIFY_S = 15
# A reading the helper does not answer within this long is given up, and so is the helper.
READ_S = 30
MAX_REPLY = 16 * 1024 * 1024
# What the interpreter says of itself; it runs on any Python, so the refusal can name it.
_IDENTIFY = (
    "import json, platform, sys; print(json.dumps([getattr(getattr(sys, 'implementation', "
    "None), 'name', platform.python_implementation().lower()), platform.python_version(), "
    "list(sys.version_info[:2])]))"
)
_SERVE = (
    "import sys; sys.path.insert(0, sys.argv.pop(1)); "
    "from peeky.python_interpreter import serve; sys.exit(serve())"
)


@dataclass(frozen=True)
class Interpreter:
    """``path``: what runs (absolute, its links not followed: a virtual environment's
    `python` is a link to the one it was made from); ``chosen``: a key of CHOSEN;
    ``version``: `3.12.4`; ``unused``: a virtual environment above the root, which is
    never used, named where Peeky's own runs the program."""

    path: str
    chosen: str
    version: str
    unused: str | None = None

    def identity(self):
        """The run's `python`: the interpreter, its version, how it was chosen, and
        where its links lead (reported only)."""
        found = {"interpreter": self.path, "version": self.version, "chosen": self.chosen}
        if (resolved := os.path.realpath(self.path)) != self.path:
            found["resolved"] = resolved
        if self.unused:
            found["unused"] = self.unused
        return found


def text(identity):
    """`/p/.venv/bin/python, Python 3.12.4 (the root's .venv)` for a run's `python`."""
    if not isinstance(identity, dict) or not identity.get("interpreter"):
        return None
    how = CHOSEN.get(identity.get("chosen"), str(identity.get("chosen")))
    if identity.get("unused"):
        how += (
            f"; {identity['unused']} is above the root, where Peeky does not look: "
            f"--python {identity['unused']}/bin/python uses it"
        )
    return f"{identity['interpreter']}, Python {identity.get('version')} ({how})"


def _environment_python(directory):
    python = Path(directory) / "bin" / "python"
    if python.is_file() and os.access(python, os.X_OK):
        return str(python)
    return None


def choose(root, option=None, environ=None):
    """``(path, chosen, unused)``: which interpreter runs a program under ``root``
    (see the module's docstring); nothing is run. ValueError for a ``--python`` that
    names no executable file."""
    environ = os.environ if environ is None else environ
    if option == OWN:
        return sys.executable, "peeky", None
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
        return found, "--python", None
    root = Path(root)
    for name in ENVIRONMENTS:
        if (root / name / "pyvenv.cfg").is_file() and (found := _environment_python(root / name)):
            return found, name, None
    active = environ.get("VIRTUAL_ENV")
    if (
        active
        and os.path.isabs(active)
        and os.path.realpath(active) != os.path.realpath(sys.prefix)
        and (found := _environment_python(active))
    ):
        return found, "VIRTUAL_ENV", None
    unused = next(
        (
            str(parent / name)
            for parent in root.resolve().parents
            for name in ENVIRONMENTS
            if (parent / name / "pyvenv.cfg").is_file() and _environment_python(parent / name)
        ),
        None,
    )
    return sys.executable, "peeky", unused


def own_version():
    import platform

    return platform.python_version()


def identify(path, chosen, unused=None):
    """The Interpreter at ``path``, asked its implementation and version with no
    credentials in its environment; ValueError where it cannot be asked, or is not
    CPython 3.11 or later."""
    shown = f"{path} ({CHOSEN.get(chosen, chosen)})"
    if path == sys.executable:
        found = [sys.implementation.name, own_version(), list(sys.version_info[:2])]
    else:
        try:
            ran = subprocess.run(
                [path, "-I", "-c", _IDENTIFY],
                capture_output=True,
                text=True,
                timeout=IDENTIFY_S,
                check=False,
                stdin=subprocess.DEVNULL,
                env=without_credentials(os.environ),
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise ValueError(
                f"Peeky could not run the Python interpreter {shown}: {exc}. Pass the "
                "program's interpreter with --python, or --python peeky for Peeky's own"
            ) from None
        try:
            found = json.loads(split_lines(ran.stdout.strip())[-1]) if ran.returncode == 0 else None
            implementation, version, (major, minor) = found
            found = [str(implementation), str(version), [int(major), int(minor)]]
        except (ValueError, TypeError, IndexError):
            said = split_lines((ran.stderr or ran.stdout or "").strip())
            raise ValueError(
                f"Peeky could not ask the Python interpreter {shown} its version"
                + (f" ({said[-1][:200]})" if said else f" (exit status {ran.returncode})")
                + ". Pass the program's interpreter with --python, or --python peeky for "
                "Peeky's own"
            ) from None
    implementation, version, release = found
    if implementation != "cpython" or tuple(release) < MINIMUM:
        name = "CPython" if implementation == "cpython" else implementation
        raise ValueError(
            f"Peeky runs Python programs on CPython {MINIMUM[0]}.{MINIMUM[1]} or later, and "
            f"{shown} is {name} {version}: Peeky reads the program's code in its "
            f"interpreter, and reads only what CPython {MINIMUM[0]}.{MINIMUM[1]} and later "
            f"compile. Pass --python with CPython {MINIMUM[0]}.{MINIMUM[1]} or later, or "
            f"--python peeky for Peeky's own (Python {own_version()})"
        )
    return Interpreter(path, chosen, version, unused)


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


# The readings, as plain JSON values both here and in a helper.


def _stale_note(path, function, line, name, loaded_by, entered):
    from peeky.python_passes import stale_note

    found = stale_note(path, function, line, name, loaded_by=loaded_by, entered=entered)
    return None if found is None else [str(found), found.note]


def _classify(path, function, line, names, listed, loaded_by):
    from peeky.python_scopes import classify

    found, reason = classify(path, function, line, names, listed, loaded_by=loaded_by)
    if found is None:
        return [None, reason]
    return [{name: None if how is None else list(how) for name, how in found.items()}, None]


def _entry_lines(program, lines):
    from peeky.adapters import python_entry_lines

    return [[line, first] for line, first in python_entry_lines(program, lines).items()]


def _lines(path):
    from peeky.python_scopes import breakable_lines

    return breakable_lines(Path(path))


def _scopes(path):
    from peeky.python_scopes import function_scopes

    return function_scopes(Path(path))


def _self_contained(text):
    from peeky.adapters import _self_contained

    return _self_contained(text)


_READINGS = {
    "stale_note": _stale_note,
    "classify": _classify,
    "entry_lines": _entry_lines,
    "lines": _lines,
    "scopes": _scopes,
    "self_contained": _self_contained,
}


class Unavailable(Exception):
    """The program's interpreter could not make a reading: nothing is read instead."""


def serve():
    """The helper: one JSON request per line on stdin, `{"id", "reading", "args"}`; one
    reply per line on stdout, `{"id", "result"}`, `{"id", "error"}` (the reading's own
    refusal, a ValueError) or `{"id", "failed"}`. Ends at the end of its input."""
    replies = sys.stdout
    # Whatever a reading or the environment's startup prints is not a reply.
    sys.stdout = sys.stderr
    for line in sys.stdin:
        try:
            request = json.loads(line)
            number = request["id"]
        except (ValueError, TypeError, KeyError):
            continue
        try:
            reply = {"id": number, "result": _READINGS[request["reading"]](*request["args"])}
        except ValueError as exc:
            reply = {"id": number, "error": str(exc)}
        except Exception as exc:
            reply = {"id": number, "failed": f"{type(exc).__name__}: {exc}"[:500]}
        replies.write(json.dumps(reply) + "\n")
        replies.flush()
    return 0


class Readings:
    """Readings of a program's code by its interpreter, one at a time."""

    def __init__(self, interpreter):
        self.interpreter = interpreter
        self.lock = threading.Lock()
        self.process = None
        self.buffer = b""
        self.asked = 0
        self.failure = None

    def __call__(self, reading, *args, timeout=READ_S):
        """The reading's JSON value; ValueError for its own refusal; Unavailable where
        the interpreter could not make it (within ``timeout`` seconds)."""
        from peeky.python_passes import same_compiler

        if same_compiler(self.interpreter, timeout=min(timeout, IDENTIFY_S)):
            return json.loads(json.dumps(_READINGS[reading](*args)))
        with self.lock:
            if self.failure is not None:
                raise Unavailable(self.failure)
            try:
                return self._ask(reading, list(args), timeout)
            except Unavailable as exc:
                self.failure = str(exc)
                self._stop()
                raise

    def _start(self):
        import peeky

        try:
            self.process = subprocess.Popen(
                [self.interpreter, "-I", "-B", "-c", _SERVE]
                + [str(Path(peeky.__file__).resolve().parents[1])],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                env=without_credentials(os.environ),
            )
        except OSError as exc:
            raise Unavailable(f"it could not be started: {exc}") from None

    def _ask(self, reading, args, timeout):
        if self.process is None:
            self._start()
        self.asked += 1
        number = self.asked
        try:
            self.process.stdin.write(
                (json.dumps({"id": number, "reading": reading, "args": args}) + "\n").encode()
            )
            self.process.stdin.flush()
        except OSError:
            raise Unavailable("it ended") from None
        deadline = time.monotonic() + timeout
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
                if "result" in reply:
                    return reply["result"]
                if "error" in reply:
                    raise ValueError(str(reply["error"]))
                raise Unavailable(str(reply.get("failed"))[:300])
            if len(self.buffer) > MAX_REPLY:
                raise Unavailable("its reply was too long")
            left = deadline - time.monotonic()
            if left <= 0:
                raise Unavailable(f"it did not answer within {round(timeout, 1)} s")
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
        process, self.process = self.process, None
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
        with self.lock:
            self._stop()


_ALL = {}
_ALL_LOCK = threading.Lock()


def readings(interpreter):
    """The Readings of ``interpreter`` (one helper per interpreter for this process)."""
    with _ALL_LOCK:
        if interpreter not in _ALL:
            _ALL[interpreter] = Readings(interpreter)
        return _ALL[interpreter]


@atexit.register
def close_all():
    with _ALL_LOCK:
        for each in _ALL.values():
            each.close()
        _ALL.clear()
