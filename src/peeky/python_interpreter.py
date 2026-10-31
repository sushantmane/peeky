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
this process only where the interpreter's own file is this one's (`os.path.samefile`),
and it reports this compiler (implementation, exact version and build, ABI flags, cache
tag, magic number) and optimization level; else in a helper process of it, which imports
this Peeky by absolute path (`-I`) and reports its compiler first. A reading that depends
on the environment (what a module import finds) is always the helper's. No reading made
on one version is applied to another.

An interpreter is identified once per launch, run as the program will be: in the
directory debugpy starts it in (the root: a pyenv shim picks its Python from there), with
the program's environment. It reports the file it runs (`sys.executable`), which debugpy
then runs, and which the helper runs: a shim can't choose another Python later. Where
that file is not the path given, it is asked too, run alone; a wrapper whose file then
reports other flags (`-O`), another import path (an exported PYTHONPATH) or other PYTHON*
variables (PYTHONWARNINGS) is refused, as debugpy would run it without them. Other
variables are not compared (a shim may change PATH), and a wrapper's are not kept. That file's place, inode, size and mtime are checked
at each reading. What it prints is
read only from a record of its own, after a newline and with a nonce of the call's, so
a `.pth` file's or an exit handler's output can't be taken for it. A reading that can't
be made is not remembered: the next one asks again. A launch's readings, and their
helper, are its adapter's, closed with the launch. The interpreter must be CPython 3.11
or later, as Peeky is; another one is refused before anything runs.
"""

import ast
import atexit
import importlib.util
import json
import os
import re
import secrets
import select
import shutil
import signal
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
# What identify keeps of what the interpreter prints, its record and anything else.
MAX_IDENTIFY_OUTPUT = 1024 * 1024
# What the interpreter says of itself, run as the program is (no -I, the program's
# environment) but importing nothing but builtin modules: any Python can run it, so a
# refusal names it. Its record follows a newline and names the call's nonce (argv[1]): a
# `.pth` file's output without a newline, or an exit handler's after it, is not part of
# it. It ends with every PYTHON* variable of its environment, as bytes.
_IDENTIFY = (
    "import sys, posix\n"
    "i = getattr(sys, 'implementation', None)\n"
    "m = getattr(sys.modules.get('_frozen_importlib_external'), 'MAGIC_NUMBER', None)\n"
    "sys.stdout.write('\\n<PEEKY-IDENTITY %s> %r\\n' % (sys.argv[1], ["
    "getattr(i, 'name', 'cpython'), sys.version.split()[0], list(sys.version_info[:2]), "
    "sys.version, getattr(sys, 'abiflags', ''), getattr(i, 'cache_tag', None), "
    "m.hex() if m else '', sys.flags.optimize, sys.executable, repr(sys.flags), "
    "list(sys.path), sorted((k, v) for k, v in posix.environ.items() "
    "if k.startswith(b'PYTHON'))]))\n"
    "sys.stdout.flush()\n"
)
_SERVE = (
    "import sys; sys.path.insert(0, sys.argv.pop(1)); "
    "from peeky.python_interpreter import serve; sys.exit(serve())"
)
# Readings that depend on the environment (what a module import finds): never made in
# this process, whose environment is Peeky's, even where its interpreter is the program's.
ENVIRONMENTAL = frozenset({"self_contained", "installed_dirs"})


def record(marker, nonce, text):
    """The text of the record ``marker`` with ``nonce`` that ``text`` holds (after a
    newline, up to the next), or None."""
    start = f"\n<{marker} {nonce}> "
    at = text.find(start)
    if at < 0:
        return None
    end = text.find("\n", at + len(start))
    return None if end < 0 else text[at + len(start) : end]


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
    """``path``: what was chosen (absolute, its links not followed: a virtual
    environment's `python` is a link to the one it was made from); ``chosen``: a key of
    CHOSEN; ``version``: `3.12.4`; ``optimize``: the program's `sys.flags.optimize`;
    ``compiler``: what it reported (own_compiler); ``executable``: the file it reported
    running (`sys.executable`), which runs the program and the helper; ``fingerprint``:
    that file when it was asked; ``cwd``: where it was asked, the directory debugpy starts
    it in; ``unused``: a virtual environment above the root, which is never used, named
    where Peeky's own runs the program; ``environment``: Peeky's own virtual environment,
    where that is the one above the root."""

    path: str
    chosen: str
    version: str
    optimize: int = 0
    compiler: tuple = ()
    executable: str = ""
    fingerprint: tuple = ()
    cwd: str | None = None
    unused: str | None = None
    environment: str | None = None

    @property
    def same(self):
        """Whether readings may be made in this process: its file is this interpreter's,
        and it reports this compiler and optimization level (each compile is given the
        program's level anyway)."""
        try:
            shared = os.path.samefile(self.executable, sys.executable)
        except (OSError, ValueError):
            return False
        return (
            shared and list(self.compiler) == own_compiler() and self.optimize == sys.flags.optimize
        )

    def current(self):
        """Whether its file is still the one it was asked as."""
        try:
            return fingerprint(self.executable) == list(self.fingerprint)
        except (OSError, ValueError):
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
        if self.executable and self.executable != self.path:
            found["executable"] = self.executable
        if (resolved := os.path.realpath(self.executable or self.path)) != self.path:
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
    runs = f" (runs {identity['executable']})" if identity.get("executable") else ""
    return f"{identity['interpreter']}{runs}, Python {identity.get('version')}{flags} ({how})"


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


def _bounded_run(command, cwd, env, timeout, limit):
    """(exit status, stdout, stderr) of ``command``, each stream at most ``limit``
    bytes: one that prints more, or runs past ``timeout`` seconds, is ended with its
    process group, and OSError raised."""
    process = subprocess.Popen(
        command,
        cwd=cwd,
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
    )
    streams = {process.stdout: bytearray(), process.stderr: bytearray()}
    deadline, problem = time.monotonic() + timeout, None
    try:
        while streams and problem is None:
            left = deadline - time.monotonic()
            open_streams = [stream for stream in streams if not stream.closed]
            if not open_streams:
                break
            if left <= 0:
                problem = f"it ran past {timeout} s"
                break
            ready, _, _ = select.select(open_streams, [], [], left)
            for stream in ready:
                chunk = os.read(stream.fileno(), 65536)
                if not chunk:
                    stream.close()
                    continue
                streams[stream] += chunk
                if len(streams[stream]) > limit:
                    problem = f"it printed over {limit >> 10} KiB"
        if problem is None:
            try:
                status = process.wait(timeout=max(0.1, deadline - time.monotonic()))
            except subprocess.TimeoutExpired:
                problem = f"it ran past {timeout} s"
    finally:
        if problem is not None or process.poll() is None:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except OSError:
                pass
            process.wait()
        for stream in streams:
            if not stream.closed:
                stream.close()
    if problem is not None:
        raise OSError(problem)
    out, err = streams.values()
    return status, bytes(out), bytes(err)


def _flags(text):
    """{name: value} of a `repr(sys.flags)`."""
    return dict(re.findall(r"(\w+)=([^,)]*)", str(text)))


def _ask(path, cwd):
    """What ``path`` says of itself, run as the program is, in ``cwd``: (its record as
    a list, or None, the exit status, its last line of error output or output)."""
    nonce = secrets.token_hex(8)
    status, out, err = _bounded_run(
        [path, "-c", _IDENTIFY, nonce], cwd, program_environment(), IDENTIFY_S, MAX_IDENTIFY_OUTPUT
    )
    text = out.decode(errors="replace")
    said = record("PEEKY-IDENTITY", nonce, text) if status == 0 else None
    try:
        said = ast.literal_eval(said) if said is not None else None
    except (ValueError, TypeError, SyntaxError, MemoryError, RecursionError):
        said = None
    lines = split_lines((err.decode(errors="replace") or text).strip())
    return said, status, lines[-1][:200] if lines else ""


def _differences(through, direct):
    """What a wrapper changes: how its record differs from that of the file it runs,
    run directly."""
    found = []
    if through[:7] != direct[:7]:
        found.append("its build")
    flags, alone = _flags(through[9]), _flags(direct[9])
    found += [
        f"sys.flags.{name} {flags.get(name)}, not {alone.get(name)}"
        for name in sorted(set(flags) | set(alone))
        if flags.get(name) != alone.get(name)
    ]
    if through[10] != direct[10]:
        found.append("its import path, sys.path")
    # Only Python's own variables: a shim may change PATH and others, which no Python
    # reads as one of its settings.
    variables, alone = dict(through[11]), dict(direct[11])
    found += [
        f"the environment variable {name.decode(errors='replace')}"
        for name in sorted(set(variables) | set(alone))
        if variables.get(name) != alone.get(name)
    ]
    return found


def identify(path, chosen, unused=None, environment=None, cwd=None):
    """The Interpreter at ``path``: asked, as the program will be run (in ``cwd``, the
    directory debugpy starts it in, with its environment, which has no credentials), for
    its implementation, version, compiler, optimization level, flags, import path and the
    file it runs. That file is what debugpy runs: where ``path`` is another (a shim, a
    wrapper), the file is asked too, run directly, and must say the same, flags and
    import path included. ValueError where it cannot be asked, changes while asked, is a
    wrapper that changes how Python runs, or is not CPython 3.11 or later."""
    shown = f"{path} ({CHOSEN.get(chosen, chosen)})"
    cwd = os.path.abspath(cwd or os.getcwd())

    def unrunnable(why):
        return ValueError(
            f"Peeky could not run the Python interpreter {shown}: {why}. Pass the "
            "program's interpreter with --python, or --python peeky for Peeky's own"
        )

    try:
        before = fingerprint(path)
        said, status, last = _ask(path, cwd)
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        raise unrunnable(exc) from None
    try:
        implementation, version, (major, minor), build, flags, tag, magic, level = said[:8]
        executable, _, _, variables = said[8:]
        if not all(isinstance(each, tuple) and len(each) == 2 for each in variables):
            raise ValueError("no environment")
        if not isinstance(executable, str) or not os.path.isabs(executable):
            raise ValueError("no executable")
        release = (int(major), int(minor))
    except (ValueError, TypeError, IndexError):
        raise ValueError(
            f"Peeky could not ask the Python interpreter {shown} its version"
            + (f" ({last})" if last else f" (exit status {status})")
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
    if executable != path:
        # debugpy runs the file itself: what a wrapper adds (`-O`, PYTHONPATH) is lost.
        try:
            direct, _, why = _ask(executable, cwd)
        except (OSError, ValueError, subprocess.SubprocessError) as exc:
            direct, why = None, str(exc)
        if not isinstance(direct, list) or len(direct) != len(said):
            changes = [f"it does not run alone ({why or 'no answer'})"]
        else:
            changes = _differences(said, direct)
            if direct[8] != executable:
                changes.append(f"run alone it runs {direct[8]}")
        if changes:
            raise ValueError(
                f"The Python interpreter {shown} is a wrapper that changes how Python runs: "
                f"it runs {executable} with other settings than that file has alone "
                f"({'; '.join(changes)[:400]}), and debugpy runs the file itself. Pass the "
                f"interpreter itself, --python {executable}, and set those options another "
                "way, such as PYTHONOPTIMIZE or PYTHONPATH in the environment"
            )
    try:
        found = Interpreter(
            path,
            chosen,
            str(version),
            int(level),
            (str(implementation), str(build), str(flags), tag, str(magic)),
            executable,
            tuple(fingerprint(executable)),
            cwd,
            unused,
            environment,
        )
        changed = fingerprint(path) != before
    except (OSError, ValueError, TypeError):
        changed = True
    if changed or not found.current():
        raise unrunnable("it changed while Peeky asked it its version; start again")
    return found


# Where the interpreter finds a module that `python -m` runs: as debugpy's launch runs it,
# in the root, with the program's environment, the root first on its path (as `-c` puts
# it). Its record follows a newline and names the call's nonce (argv[1]).
_FIND = (
    "import sys, importlib.util\n"
    "s = importlib.util.find_spec(sys.argv[2])\n"
    "o = None if s is None else [s.origin, list(s.submodule_search_locations or [])]\n"
    "sys.stdout.write('\\n<PEEKY-MODULE %s> %r\\n' % (sys.argv[1], o))\n"
    "sys.stdout.flush()\n"
)


def module_origin(interpreter, name):
    """``[origin, search locations]`` of the top-level module ``name`` as ``interpreter``
    (an Interpreter) finds it for `python -m`: run as the program is, in its directory,
    with the program's environment. None where it finds none. ValueError where it can't
    be asked. Finding a top-level module imports nothing of it."""
    nonce = secrets.token_hex(8)
    try:
        status, out, _ = _bounded_run(
            [interpreter.executable, "-c", _FIND, nonce, name],
            interpreter.cwd,
            program_environment(),
            IDENTIFY_S,
            MAX_IDENTIFY_OUTPUT,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise ValueError(f"Peeky could not ask {interpreter.executable} ({exc})") from None
    said = record("PEEKY-MODULE", nonce, out.decode(errors="replace")) if status == 0 else None
    try:
        found = ast.literal_eval(said) if said is not None else ValueError
    except (ValueError, TypeError, SyntaxError, MemoryError, RecursionError):
        found = ValueError
    if found is not None and not (
        isinstance(found, list)
        and len(found) == 2
        and (found[0] is None or isinstance(found[0], str))
        and isinstance(found[1], list)
    ):
        raise ValueError(
            f"Peeky could not ask {interpreter.executable} where it finds {name} (exit status "
            f"{status})"
        )
    return found


def find(root, option=None, environ=None):
    """The Interpreter that runs a program under ``root`` (``choose``, then
    ``identify`` in ``root``, where debugpy starts the program)."""
    return identify(*choose(root, option, environ), cwd=root)


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


def _self_contained(optimize, text, places=()):
    from peeky.adapters import _self_contained

    return _self_contained(text, places)


def _installed_dirs(optimize):
    """Where this interpreter installs packages: its site-packages directories, its user
    site, and sysconfig's purelib and platlib (debugpy counts code there as library code).
    Absolute, from the directory the helper runs in, the program's: a relative
    PYTHONUSERBASE gives a relative user site."""
    import site
    import sysconfig

    found = []
    for name in ("getsitepackages", "getusersitepackages"):
        try:
            each = getattr(site, name)()
        except (AttributeError, OSError):
            continue
        found += [each] if isinstance(each, str) else list(each)
    paths = sysconfig.get_paths()
    found += [paths.get("purelib"), paths.get("platlib")]
    return sorted({os.path.abspath(each) for each in found if isinstance(each, str) and each})


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
    "installed_dirs": _installed_dirs,
}


class Unavailable(Exception):
    """The program's interpreter could not make a reading: nothing is read instead."""


def serve():
    """The helper: first its compiler, `{"id": 0, "compiler"}`; then one reply per
    request, `{"id", "reading", "optimize", "args"}` a JSON line on stdin; `{"id",
    "result"}`, `{"id", "error"}` (the reading's own refusal, a ValueError) or `{"id",
    "failed"}` on stdout. Each reply is a record after a newline, `<PEEKY-READING
    nonce> JSON`, with the nonce it was started with (argv[1]): what else reaches stdout
    (a `.pth` file's output at startup, an exit handler's) is not a reply. Ends at the
    end of its input."""
    replies, nonce = sys.stdout, sys.argv[1]
    # Whatever a reading prints is not a reply.
    sys.stdout = sys.stderr

    def reply(value):
        replies.write(f"\n<PEEKY-READING {nonce}> {json.dumps(value)}\n")
        replies.flush()

    reply({"id": 0, "compiler": own_compiler()})
    for line in sys.stdin:
        try:
            request = json.loads(line)
            number = request["id"]
            reading, optimize = _READINGS[request["reading"]], int(request["optimize"])
        except (ValueError, TypeError, KeyError):
            continue
        try:
            reply({"id": number, "result": reading(optimize, *request["args"])})
        except ValueError as exc:
            reply({"id": number, "error": str(exc)})
        except Exception as exc:
            reply({"id": number, "failed": f"{type(exc).__name__}: {exc}"[:500]})
    return 0


_OPEN = weakref.WeakSet()


class Readings:
    """One launch's readings of a program's code by its ``interpreter`` (identified for
    the launch; else ``path`` asked at the first reading, and again after ``close``),
    one at a time; its helper, if any, ends at ``close``."""

    def __init__(self, path, interpreter=None):
        self.path = path
        self.given = interpreter
        self.found = None
        self.lock = threading.Lock()
        self.process = None
        self.nonce = None
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
                raise Unavailable(
                    f"{interpreter.executable} is not the interpreter Peeky asked at launch"
                )
            if interpreter.same and reading not in ENVIRONMENTAL:
                return json.loads(json.dumps(_READINGS[reading](interpreter.optimize, *args)))
            try:
                return self._ask(interpreter, reading, list(args), deadline)
            except Unavailable:
                self._stop()
                raise

    def _start(self, interpreter, deadline):
        import peeky

        self.nonce = secrets.token_hex(8)
        try:
            # The file the program runs, where it runs: a shim can't pick another.
            self.process = subprocess.Popen(
                [interpreter.executable, "-I", "-B", "-c", _SERVE]
                + [str(Path(peeky.__file__).resolve().parents[1]), self.nonce],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                cwd=interpreter.cwd,
                env=without_credentials(os.environ),
            )
        except OSError as exc:
            raise Unavailable(f"it could not be started: {exc}") from None
        os.set_blocking(self.process.stdin.fileno(), False)
        said = self._reply(0, deadline)
        if said != list(interpreter.compiler):
            raise Unavailable(
                f"{interpreter.executable} is not the interpreter Peeky asked at launch (its "
                f"helper reports {str(said)[:200]})"
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
        """The value of reply ``number`` (0: the helper's compiler), read from its
        records only (``serve``)."""
        start = f"\n<PEEKY-READING {self.nonce}> ".encode()
        while True:
            at = self.buffer.find(start)
            if at >= 0:
                end = self.buffer.find(b"\n", at + len(start))
                if end >= 0:
                    text, self.buffer = self.buffer[at + len(start) : end], self.buffer[end:]
                    try:
                        reply = json.loads(text)
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
                if len(self.buffer) - at > MAX_REPLY:
                    raise Unavailable("its reply was too long")
            else:
                # Not a record: kept only where a record's start may begin.
                self.buffer = self.buffer[-len(start) :]
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
