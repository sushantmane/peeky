"""`--test`: the project's own test command names what Peeky runs (ADR 0009).

The words after `--` are the command that runs the failing test, as typed in the directory it
runs in. Its first word decides the kind:

- Python: `python -m pytest`, or a script, on the interpreter that command runs. A `pytest`
  script is refused, naming the `python -m pytest` command to give instead.
- C and C++: a binary inside the target directory (`--binary`), or the one test `ctest` names,
  where ctest would run it as Peeky runs a binary.
- Rust: `cargo test` or `cargo run`, read into a workspace configuration with `cargo
  metadata`'s help (rust_workspace), which Peeky builds when no build of it is current.
- Java: Maven, Gradle, `java` and `javac`, which java_test starts.

What runs, and every check after that, is what the equivalent options would run: `--test`
only names it. A command Peeky can't name a target from is refused before anything runs,
with what to do instead.
"""

import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from peeky.errors import Refused
from peeky.textlines import split_lines

ASSIGNMENT = re.compile(r"([A-Za-z_][A-Za-z0-9_]*)=(.*)", re.S)
_PYTHON = re.compile(r"python[0-9.]*")
_SHELLS = frozenset({"sh", "bash", "zsh", "dash", "ksh", "fish", "xargs", "nohup", "time"})
# The options `--test` takes the place of: given with it, they would name a second target.
REPLACED = ("config", "run", "run_module", "python", "binary", "arg")
_JAVA_TOOLS = frozenset({"mvn", "mvnw", "gradle", "gradlew", "java", "javac"})
_OTHER_TOOLS = {
    "make": "`make` runs a build, then whatever its rule says",
    "ninja": "`ninja` runs a build",
    "cmake": "`cmake` runs a build",
    "tox": "`tox` runs its own environments",
    "nox": "`nox` runs its own environments",
    "ant": "Ant forks its test JVMs with no switch Peeky can add its debug agent to",
    "sbt": "sbt runs its tests in its own JVM",
    "bazel": "Bazel runs its tests in its own sandbox",
    "npm": "Peeky doesn't debug JavaScript",
    "go": "Peeky doesn't debug Go",
}


@dataclass(frozen=True)
class TestCommand:
    """What a test command names. ``kind``: python, binary, rust or java. ``words``: the
    command less its environment assignments, which are ``environment``. The other fields
    are the options it stands for (None: not given). ``notes``: what Peeky says beside
    the result."""

    kind: str
    words: tuple[str, ...]
    environment: tuple[tuple[str, str], ...] = ()
    python: str | None = None
    run: str | None = None
    module: str | None = None
    binary: str | None = None
    config: dict | None = None
    arguments: tuple[str, ...] = ()
    notes: tuple[str, ...] = field(default=())

    @property
    def shown(self):
        return shlex.join((*(f"{k}={v}" for k, v in self.environment), *self.words))


def _unsupported(message):
    return Refused(
        "UNSUPPORTED", message, "Nothing ran. Change the test command as the message says."
    )


def _invalid(message):
    return Refused("INVALID_REQUEST", message)


def split_environment(words):
    """(assignments, the command's words): `A=1 B=2 cmd …`, and `env A=1 cmd …`."""
    words = list(words)
    if words[:1] == ["env"]:
        words = words[1:]
        if words[:1] and words[0].startswith("-"):
            raise _unsupported(
                f"--test runs `env` only with assignments, not {words[0]}: give the "
                "assignments and the command"
            )
    assignments = []
    while words and (found := ASSIGNMENT.fullmatch(words[0])):
        assignments.append((found[1], found[2]))
        words = words[1:]
    return tuple(assignments), tuple(words)


def _which(word, environment, cwd):
    """The file a command's first word runs: a path (relative to ``cwd``) or a name on
    PATH (the command's own PATH where it sets one)."""
    if "/" in word:
        return Path(os.path.normpath(Path(cwd) / word))
    found = shutil.which(word, path=environment.get("PATH", os.environ.get("PATH")))
    return Path(found) if found else None


def _inside(path, root):
    try:
        Path(os.path.realpath(path)).relative_to(os.path.realpath(root))
    except ValueError:
        return False
    return True


def _relative(path, root):
    return Path(os.path.realpath(path)).relative_to(os.path.realpath(root)).as_posix()


def plan(words, root, program=None, cwd=None):
    """The TestCommand that ``words`` names, for the target directory ``root`` and the
    `--at` file ``program`` (relative to the root, or None)."""
    test = _plan(words, root, program, cwd)
    if test.kind in ("binary", "rust"):
        test = _own_environment(test)
    return test


_LINT_OPTIONS = frozenset({"-A", "-W", "-D", "-F", "--cap-lints", "--force-warn"})
_LINT_WORD = re.compile(r"-[AWDF]\S+|--(?:cap-lints|force-warn)=\S+")


def _lints_only(flags):
    """Whether rustc ``flags`` (a list) set only lints (`-Awarnings`, `--cap-lints warn`)."""
    words = list(flags)
    while words:
        word = words.pop(0)
        if word in _LINT_OPTIONS:
            if not words:
                return False
            words.pop(0)
        elif not _LINT_WORD.fullmatch(word):
            return False
    return True


# What a binary --test runs gets of the environment: these (build.INHERITED_ENV), and a
# HOME of Peeky's own, the debugger's. A Rust test gets none of it (rust_workspace_session).
_REACHES_BINARY = ("PATH", "TMPDIR", "TMP", "TEMP", "LANG", "LC_ALL")
_BINARY_ENVIRONMENT = (
    "Peeky runs {binary} in an environment of its own: of yours it passes only PATH, TMPDIR, "
    "TMP, TEMP, LANG and LC_ALL, and HOME is a directory of Peeky's. A test that reads another "
    "of your variables, or HOME, may run otherwise"
)
_RUST_ENVIRONMENT = (
    "Peeky builds the test with Cargo, and runs its executable itself, in environments of its "
    "own: Cargo and build scripts get none of your variables, and the test only PATH (the "
    "toolchain's directory, /usr/bin and /bin), LC_ALL=C, a HOME of Peeky's, "
    "CARGO_MANIFEST_DIR and, in an integration test, CARGO_BIN_EXE_<name>. `cargo test` would "
    "pass on your environment, and more CARGO_* variables: a test or build script that reads "
    "one may run otherwise"
)


def _own_environment(test):
    """A C, C++ or Rust test command's environment: Peeky runs the binary, and Cargo and the
    test, in an environment of its own, so an assignment that wouldn't reach them is
    refused (Cargo's own variables: _cargo_variables), and a note says what the program gets
    of yours."""
    from dataclasses import replace

    rust = test.kind == "rust"
    for name, _ in test.environment:
        if name in (_FLAGS if rust else _REACHES_BINARY):
            continue
        raise _unsupported(
            f"Peeky runs {'Cargo and the test' if rust else 'the binary'} in an environment of "
            f"its own, so {name}=… in the test command wouldn't reach "
            + ("them" if rust else "it")
            + ": drop it"
        )
    note = _RUST_ENVIRONMENT if rust else _BINARY_ENVIRONMENT.format(binary=test.binary)
    return replace(test, notes=(*test.notes, note))


def _plan(words, root, program, cwd):
    cwd = Path(cwd or os.getcwd())
    root = Path(root).resolve()
    assignments, words = split_environment(words)
    if not words:
        raise _invalid(
            "--test takes the command that runs the failing test after `--`: "
            "peeky probe . --at FILE:LINE --test -- python -m pytest tests/test_x.py::test_y"
        )
    environment = {**os.environ, **dict(assignments)}
    first = words[0]
    name = Path(first).name
    if name in _SHELLS or any(word in ("&&", "||", ";", "|") for word in words):
        raise _unsupported(
            f"--test runs one command, not a shell line ({shlex.join(words)}): give the "
            "command that runs the test itself, such as `python -m pytest tests/test_x.py::test_y`"
        )
    if name in _JAVA_TOOLS:
        return TestCommand("java", words, assignments)
    if name == "cargo":
        return _cargo(words, assignments, root, program, environment, cwd)
    if name == "ctest":
        return _ctest(words, assignments, root, environment, cwd)
    if name in ("pytest", "py.test") or _PYTHON.fullmatch(name):
        return _python(words, assignments, root, environment, cwd)
    if name in _OTHER_TOOLS:
        raise _unsupported(
            f"--test can't run `{name}`: {_OTHER_TOOLS[name]}. Give the command that runs "
            "the one failing test: its binary with its arguments (C, C++), `python -m pytest …`, "
            "`cargo test …`, or the `java` command it runs"
        )
    path = _which(first, environment, cwd)
    if path is None or not path.is_file():
        raise _invalid(f"--test's command `{first}` is not a file, nor on PATH")
    from peeky.cli import _script_python

    if _script_python(path, environment) is not None:
        return _python(words, assignments, root, environment, cwd)
    if not _inside(path, root):
        raise _unsupported(
            f"--test's program {path} is outside the target directory {root}: Peeky debugs a "
            "binary built from the sources under it. Give the target directory that holds it"
        )
    return _binary(words, assignments, path, root, cwd)


def _python(words, assignments, root, environment, cwd):
    from peeky.cli import _script_python

    first = words[0]
    path = _which(first, environment, cwd)
    if path is None or not path.is_file():
        raise _invalid(f"--test's command `{first}` is not a file, nor on PATH")
    if Path(first).name in ("pytest", "py.test"):
        raise _pytest_script(words, path, environment, cwd)
    if _PYTHON.fullmatch(path.name) and not _script_python(path, environment):
        python, rest = str(path), list(words[1:])
        options = []
        while rest and rest[0].startswith("-") and rest[0] not in ("-m", "-c", "-"):
            options.append(rest.pop(0))
        if options:
            raise _unsupported(
                f"--test can't pass the interpreter's own options ({' '.join(options)}): "
                "Peeky starts it. Set them in the environment instead, such as "
                "PYTHONWARNINGS=error for -W error, before the command"
            )
        if not rest or rest[0] in ("-c", "-"):
            raise _unsupported(
                "--test runs a test: `python -m pytest …` or `python script.py …`, not "
                "`python -c` or a program on stdin"
            )
        if rest[0] == "-m":
            module = rest[1] if len(rest) > 1 else ""
            if module == "pytest":
                _same_directory(cwd, root, "pytest")
            else:
                raise _unsupported(
                    f"--test runs `python -m pytest` only, not `-m {module}`: Peeky runs "
                    "pytest as a module and no other. Run the module from a script: run.py "
                    f"holding `import runpy; runpy.run_module({module!r}, run_name='__main__', "
                    "alter_sys=True)`, then `--test -- python run.py ARGS`"
                )
            return TestCommand(
                "python",
                words,
                assignments,
                python=python,
                module="pytest",
                arguments=tuple(rest[2:]),
            )
        script, arguments = rest[0], tuple(rest[1:])
    else:
        found = _script_python(path, environment)
        if found is None:
            raise _unsupported(f"--test's command {path} is not a Python script")
        python, options = found
        if options:
            raise _unsupported(
                f"{path} runs Python with options ({' '.join(options)}) Peeky can't pass: "
                f"give `--test -- python {first} …`"
            )
        python = str(_which(python, environment, cwd) or python)
        script, arguments = str(path), tuple(words[1:])
    script_path = Path(os.path.normpath(cwd / script))
    if not script_path.is_file():
        raise _invalid(f"--test's script {script} is not a file")
    if not _inside(script_path, root):
        raise _unsupported(
            f"--test's script {script_path} is outside the target directory {root}: give the "
            "directory that holds it as the target"
        )
    _same_directory(cwd, root, "the script")
    return TestCommand(
        "python",
        words,
        assignments,
        python=python,
        run=_relative(script_path, root),
        arguments=arguments,
    )


def _pytest_script(words, path, environment, cwd):
    """The refusal of a `pytest` (or `py.test`) script: Peeky runs pytest only as
    `python -m pytest`, which neither runs the script's own lines nor imports as it does."""
    from peeky.cli import _script_python

    found = _script_python(path, environment)
    python = str(_which(found[0], environment, cwd) or found[0]) if found else "python"
    return _unsupported(
        f"--test runs pytest only as `python -m pytest`, not as the `{words[0]}` script "
        f"({path}): the script's own lines wouldn't run, and `python -m pytest` puts the "
        "current directory first on sys.path, so it may import other code. Give the "
        "interpreter you mean: "
        f"`--test -- {shlex.join([python, '-m', 'pytest', *words[1:]])}`"
        + (f", the one {path}'s `#!` line names" if found else "")
    )


def _same_directory(cwd, root, what):
    if os.path.realpath(cwd) != os.path.realpath(root):
        raise _unsupported(
            f"Peeky runs {what} in the target directory ({root}), and this command runs in "
            f"{cwd}: run it from the target directory, or give that directory as the target"
        )


def _binary(words, assignments, path, root, cwd):
    try:
        with open(path, "rb") as stream:
            script = stream.read(2) == b"#!"
    except OSError:
        script = False
    if script:
        raise _unsupported(
            f"{words[0]} is a script: Peeky debugs the binary it runs. Give --test that binary "
            "with its arguments"
        )
    _same_directory(cwd, root, "the binary")
    return TestCommand(
        "binary", words, assignments, binary=_relative(path, root), arguments=tuple(words[1:])
    )


# ctest's options --test takes: those that select the test (each takes a value), and those
# that change only what ctest prints. Every other one changes how ctest runs tests (--repeat,
# --schedule-random, -j, --timeout, --stop-on-failure, a preset, a dashboard …), which Peeky,
# running the one test's command once, wouldn't do.
_CTEST_SELECTS = frozenset(
    {
        "-R",
        "--tests-regex",
        "-E",
        "--exclude-regex",
        "-L",
        "--label-regex",
        "-LE",
        "--label-exclude",
        "-C",
        "--build-config",
        "--test-dir",
    }
)
_CTEST_PRINTS = frozenset(
    {"-V", "--verbose", "-VV", "--extra-verbose", "-Q", "--quiet", "--output-on-failure"}
)
# A test's properties that don't change how its command runs: how ctest orders, judges,
# labels or schedules it. WORKING_DIRECTORY, TIMEOUT and REQUIRED_FILES are checked; any
# other (ENVIRONMENT, ENVIRONMENT_MODIFICATION, DISABLED, RESOURCE_GROUPS, …) is refused.
_CTEST_PROPERTIES = frozenset(
    {
        "ATTACHED_FILES",
        "ATTACHED_FILES_ON_FAIL",
        "COST",
        "DEPENDS",
        "FAIL_REGULAR_EXPRESSION",
        "FIXTURES_CLEANUP",
        "FIXTURES_REQUIRED",
        "FIXTURES_SETUP",
        "LABELS",
        "MEASUREMENT",
        "PASS_REGULAR_EXPRESSION",
        "PROCESSORS",
        "RESOURCE_LOCK",
        "RUN_SERIAL",
        "SKIP_REGULAR_EXPRESSION",
        "SKIP_RETURN_CODE",
        "WILL_FAIL",
    }
)


def _ctest(words, assignments, root, environment, cwd):
    """The one test `ctest`'s own words select, from `ctest --show-only=json-v1`. Refused
    where ctest would run it otherwise than Peeky runs a binary: another working directory,
    an environment of the test's own, or an option or property that changes the run."""
    rest = list(words[1:])
    while rest:
        word = rest.pop(0)
        if word in ("-N", "--show-only") or word.startswith("--show-only="):
            raise _invalid("Give --test the ctest command that runs the test, without -N")
        if word in _CTEST_SELECTS:
            if not rest:
                raise _invalid(f"ctest's {word} takes a value")
            rest.pop(0)
        elif word not in _CTEST_PRINTS:
            raise _unsupported(
                f"--test can't take ctest's {word}: Peeky runs the one test's command once, as "
                "ctest lists it, and doesn't do what that option does. Select the test with -R, "
                "-E, -L, -LE, -C and --test-dir only"
            )
    try:
        listed = subprocess.run(
            [*words, "--show-only=json-v1"],
            cwd=cwd,
            env=environment,
            capture_output=True,
            timeout=60,
        )
        tests = json.loads(listed.stdout).get("tests") or []
    except (OSError, ValueError, subprocess.TimeoutExpired) as exc:
        raise _unsupported(
            f"`{shlex.join([*words, '--show-only=json-v1'])}` didn't list the tests ({exc}): "
            "give the test's binary and arguments instead"
        ) from None
    if len(tests) != 1:
        names = ", ".join(str(test.get("name")) for test in tests[:8])
        raise _unsupported(
            f"ctest selects {len(tests)} tests{' (' + names + ')' if names else ''}: Peeky "
            "runs one. Select one test with -R '^NAME$'"
        )
    test = tests[0]
    name = test.get("name")
    command = [str(word) for word in test.get("command") or ()]
    if not command:
        raise _unsupported(f"ctest names no command for test {name}")
    path = Path(command[0])
    if not path.is_file() or not _inside(path, root):
        raise _unsupported(
            f"ctest's test {name} runs {command[0]}, which isn't a file in the "
            f"target directory {root}"
        )
    properties = {p.get("name"): p.get("value") for p in test.get("properties") or ()}
    notes = []
    for key, value in sorted(properties.items(), key=lambda item: str(item[0])):
        if key in _CTEST_PROPERTIES or value in (None, "", [], False):
            continue
        if key == "WORKING_DIRECTORY":
            if os.path.realpath(str(value)) != os.path.realpath(root):
                raise _unsupported(
                    f"ctest runs {name} in {value}, and Peeky runs a binary in the target "
                    f"directory ({root}), where a relative path the test reads may name "
                    "another file. Give --test the test's binary and arguments, if it runs "
                    "the same from there"
                )
        elif key in ("TIMEOUT", "TIMEOUT_AFTER_MATCH"):
            notes.append(
                f"ctest would stop {name} at its {key} ({value}); Peeky stops it only at "
                "its own --timeout"
            )
        elif key == "REQUIRED_FILES":
            where = properties.get("WORKING_DIRECTORY") or cwd
            missing = [f for f in value if not Path(where, str(f)).exists()]
            if missing:
                raise _unsupported(
                    f"ctest wouldn't run {name}: its REQUIRED_FILES {missing[0]} doesn't exist"
                )
        elif key == "DISABLED":
            raise _unsupported(f"ctest doesn't run {name}: it is DISABLED")
        else:
            shown = value if isinstance(value, str) else json.dumps(value)
            why = (
                "Peeky runs a binary in an environment of its own, and doesn't set a test's"
                if key.startswith("ENVIRONMENT")
                else "Peeky doesn't run a binary as that property says"
            )
            raise _unsupported(
                f"ctest runs {name} with its {key} {shown}: {why}. Give --test the test's "
                "binary and arguments, if it runs the same without it"
            )
    return TestCommand(
        "binary",
        tuple(words),
        assignments,
        binary=_relative(path, root),
        arguments=tuple(command[1:]),
        notes=tuple(notes),
    )


# cargo test's and cargo run's options: (option, takes a value).
_CARGO_IGNORED = {
    "-q": False,
    "--quiet": False,
    "-v": False,
    "-vv": False,
    "--verbose": False,
    "--offline": False,
    "--locked": False,
    "--frozen": False,
    "--no-fail-fast": False,
    "-j": True,
    "--jobs": True,
    "--color": True,
    "--target-dir": True,
    "--message-format": True,
}
_CARGO_REFUSED = {
    "--release": "Peeky builds at opt-level 0",
    "-r": "Peeky builds at opt-level 0",
    "--profile": "Peeky builds with its own profile, at opt-level 0",
    "--doc": "Peeky doesn't run doctests",
    "--workspace": "Peeky runs one test target",
    "--all": "Peeky runs one test target",
    "--exclude": "Peeky runs one test target",
    "--tests": "Peeky runs one test target: name it with --test NAME or --lib",
    "--bins": "Peeky runs one binary: name it with --bin NAME",
    "--examples": "Peeky doesn't run examples",
    "--example": "Peeky doesn't run examples",
    "--benches": "Peeky doesn't run benchmarks",
    "--bench": "Peeky doesn't run benchmarks",
    "--all-targets": "Peeky runs one test target: name it with --test NAME or --lib",
    "--no-default-features": "Peeky's configuration has no field for it yet",
    "--target": "Peeky builds for this host",
    "--no-run": "Peeky runs the test",
    "--manifest-path": "Peeky reads the workspace in the target directory: give its directory "
    "as the target",
    "-Z": "Peeky builds with its own toolchain, on stable Rust",
}


def _cargo(words, assignments, root, program, environment, cwd):
    rest = list(words[1:])
    if rest and rest[0].startswith("+"):
        raise _unsupported(
            f"Peeky builds with its own Rust toolchain ($PEEKY_RUST_CARGO), not {rest[0]}: drop it"
        )
    sub = rest.pop(0) if rest else ""
    if sub not in ("test", "t", "run", "r"):
        raise _unsupported(f"--test runs `cargo test` or `cargo run`, not `cargo {sub}`".rstrip())
    running = sub in ("run", "r")
    package, kind, name, features, filters, harness = None, None, None, [], [], []
    all_features = False
    while rest:
        word = rest.pop(0)
        option, _, value = word.partition("=")
        has_value = "=" in word

        def take():
            if has_value:
                return value
            if not rest:
                raise _invalid(f"cargo's {option} takes a value")
            return rest.pop(0)

        if word == "--":
            harness = rest
            break
        if option in _CARGO_REFUSED or word.startswith("-Z"):
            reason = _CARGO_REFUSED.get(option, _CARGO_REFUSED["-Z"])
            raise _unsupported(f"--test can't take `cargo {sub} {option}`: {reason}")
        if option in _CARGO_IGNORED:
            if _CARGO_IGNORED[option]:
                take()
            continue
        if option in ("-p", "--package"):
            if package is not None:
                raise _unsupported("Peeky runs one package's test: give one -p")
            package = take()
        elif option == "--bin" and not running:
            raise _unsupported(
                "--test runs `cargo test --bin` as Peeky runs a binary, not its unit tests: "
                "for the program, give `cargo run --bin NAME -- ARGS`"
            )
        elif option in ("--test", "--bin"):
            if kind is not None:
                raise _unsupported("Peeky runs one test target: give one of --test, --bin, --lib")
            kind, name = option[2:], take()
        elif option == "--lib":
            if kind is not None:
                raise _unsupported("Peeky runs one test target: give one of --test, --bin, --lib")
            kind = "lib"
        elif option in ("-F", "--features"):
            features += [f for f in re.split(r"[ ,]+", take()) if f]
        elif option == "--all-features":
            all_features = True
        elif word.startswith("-"):
            raise _unsupported(
                f"--test doesn't know cargo's {option}: drop it, or run without --test"
            )
        elif running:
            raise _invalid(f"`cargo run` takes the program's arguments after `--`, not {word}")
        else:
            filters.append(word)
    if len(filters) > 1:
        raise _invalid(f"cargo test takes one test name filter, not {len(filters)}")
    if running and kind not in (None, "bin"):
        raise _unsupported("`cargo run` runs a binary: name it with --bin NAME")
    manifests, selected = _packages(root)
    analysed = _package_of(program, root, manifests)
    notes = []
    if package is not None and package not in manifests:
        raise _unsupported(f"-p {package} names no package of the workspace under {root}")
    # What Cargo acts on: -p's package, else the root's package or `default-members`.
    scope = [package] if package is not None else selected
    if kind in ("test", "bin"):
        run_package = _owner_of(manifests, scope, kind, name, sub)
    elif running:
        if len(scope) != 1:
            raise _unsupported(
                f"`cargo run` without -p or --bin runs one of {', '.join(scope)}: name it, "
                "as Cargo needs"
            )
        kind, run_package = "bin", scope[0]
        name = _default_binary(manifests, run_package)
    else:
        if analysed not in scope:
            raise _unsupported(
                f"`cargo {sub}` without -p runs {', '.join(scope)}'s tests, and {program} is "
                f"in `{analysed}`: add -p {analysed}, or name the test target with --test"
            )
        run_package = analysed
        if kind is None:
            kind, name = _target_from_file(program, root, manifests, analysed)
    run = {"lib": True} if kind == "lib" else {kind: name}
    run["package"] = run_package
    if all_features:
        features = sorted(set(features) | set(manifests[run_package]["all_features"]))
    if package is None and len(scope) > 1:
        notes.append(
            f"Peeky builds `{run_package}` alone; `cargo {sub}` without -p builds "
            f"{', '.join(scope)} together, so Cargo may unify more features than Peeky's "
            "build has"
        )
    config = {"workspace": ".", "analyse": [analysed], "run": run}
    if features:
        config["features"] = features
    if (root / "vendor").is_dir():
        config["vendor"] = "vendor"
    _same_directory(cwd, root, "Cargo")
    tables = _cargo_configs(root, environment)
    _runner(tables, environment)
    _unread_configuration(tables)
    notes += _cargo_variables(environment, assignments, tables)
    _same_toolchain(words[0], root, environment, cwd)
    return TestCommand(
        "rust",
        tuple(words),
        assignments,
        config=config,
        arguments=(*filters, *harness),
        notes=tuple(notes),
    )


# The variables that set rustc's flags for every compile. Cargo takes the first set of
# CARGO_ENCODED_RUSTFLAGS and RUSTFLAGS in place of its configuration's `rustflags`;
# CARGO_BUILD_RUSTFLAGS is `build.rustflags`.
_FLAGS = ("CARGO_ENCODED_RUSTFLAGS", "RUSTFLAGS", "CARGO_BUILD_RUSTFLAGS")
# Variables that change what Cargo builds or how it runs a test: its configuration's keys
# (CARGO_<KEY>) of the build, a target and a profile, and the compiler it runs. Those of
# where the build goes, how many jobs, and Cargo's output and network change neither.
_CARGO_CHANGES = ("CARGO_BUILD_", "CARGO_TARGET_", "CARGO_PROFILE_", "CARGO_UNSTABLE_", "__CARGO")
_CARGO_SAME = frozenset(
    {
        "CARGO_TARGET_DIR",
        "CARGO_BUILD_TARGET_DIR",
        "CARGO_BUILD_JOBS",
        "CARGO_BUILD_INCREMENTAL",
        "CARGO_BUILD_RUSTDOCFLAGS",
    }
)
_RUSTC = frozenset(
    {"RUSTC", "RUSTC_WRAPPER", "RUSTC_WORKSPACE_WRAPPER", "RUSTC_BOOTSTRAP", "RUSTUP_TOOLCHAIN"}
)
# A Cargo configuration file's keys that change neither the build nor the run: those
# rust_workspace accounts for in any file (but `profile`, which Peeky's build reads only
# from the files it reads), and a source replacement, as the lockfile pins the sources.
_HOME_SAME = frozenset(
    {
        "alias",
        "cache",
        "cargo-new",
        "credential-alias",
        "doc",
        "future-incompat-report",
        "http",
        "install",
        "net",
        "registries",
        "registry",
        "source",
        "term",
    }
)
_HOME_BUILD_SAME = frozenset({"jobs", "target-dir", "incremental", "rustdocflags"})


def _cargo_configs(root, environment):
    """[(path, table, read)] of the configuration files Cargo reads for a command run in
    ``root``: the root's and those above it, which Peeky's build reads too (``read``), and
    $CARGO_HOME's, which it reads only where it is one of those (_unread_configuration)."""
    from peeky.rust_workspace import config_paths

    paths = [(path, True) for path in config_paths(root)]
    home = Path(environment.get("CARGO_HOME") or Path(os.path.expanduser("~")) / ".cargo")
    found = next(
        (home / name for name in ("config", "config.toml") if (home / name).is_file()), None
    )
    if found is not None and not any(
        os.path.realpath(found) == os.path.realpath(path) for path, _ in paths
    ):
        paths.append((found, False))
    tables = []
    for path, read in paths:
        try:
            tables.append((path, tomllib.loads(Path(path).read_text()), read))
        except (OSError, ValueError) as exc:
            raise _unsupported(f"Peeky can't read Cargo's configuration {path}: {exc}") from None
    return tables


def _unread_configuration(tables):
    """Refuse a configuration file Cargo reads and Peeky's build doesn't ($CARGO_HOME's,
    outside the workspace) where it may change the build or the run."""
    for path, table, read in tables:
        if read:
            continue
        changes = [key for key in table if key not in _HOME_SAME and key != "build"]
        changes += [
            f"build.{key}" for key in _section(table, "build") if key not in _HOME_BUILD_SAME
        ]
        if changes:
            raise _unsupported(
                f"Cargo reads {path} for the test command, and Peeky's build doesn't: its "
                f"`{changes[0]}` may change what Cargo builds or how it runs the test. Take it "
                "out, or run without --test"
            )


def _runner(tables, environment):
    """Refuse a runner Cargo would run the test through (`target.<host>.runner`, a
    `target.'cfg(…)'` one that holds here, or CARGO_TARGET_<TRIPLE>_RUNNER): Peeky runs the
    test's executable itself."""
    for name in sorted(environment):
        if name.startswith("CARGO_TARGET_") and name.endswith("_RUNNER"):
            raise _unsupported(
                f"Cargo would run the test through {name} ({environment[name]}), and Peeky runs "
                "the test's executable itself: unset it, or run without --test"
            )
    if not any("runner" in value for _, table, _ in tables for value in _targets(table)):
        return
    from peeky.rust_workspace import _target_tables

    host, cfgs = _host()
    for path, table, _ in tables:
        try:
            applying = _target_tables(table, host, cfgs, path)
        except ValueError as exc:
            raise _unsupported(
                f"{exc}: it may name a runner Cargo would run the test through"
            ) from None
        for key, value in applying:
            if isinstance(value, dict) and "runner" in value:
                raise _unsupported(
                    f"Cargo would run the test through `target.{key}.runner` "
                    f"({value['runner']}) in {path}, and Peeky runs the test's executable "
                    "itself: take the runner out, or run without --test"
                )


def _section(table, key):
    return table.get(key) if isinstance(table.get(key), dict) else {}


def _targets(table):
    """A configuration's `[target.*]` tables, whether they apply here or not."""
    return [value for value in _section(table, "target").values() if isinstance(value, dict)]


def _toolchain_of(tool, cwd, environment):
    """(release, commit hash, host) of a Rust tool, by its `-vV`, run in ``cwd`` with
    ``environment``, as a rustup proxy picks its toolchain there; None if it can't say."""
    try:
        found = subprocess.run(
            [str(tool), "-vV"],
            cwd=cwd,
            env=environment,
            capture_output=True,
            text=True,
            timeout=60,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    fields = dict(line.partition(": ")[::2] for line in split_lines(found.stdout) if ": " in line)
    if found.returncode != 0 or not all(fields.get(k) for k in ("release", "commit-hash", "host")):
        return None
    return fields["release"], fields["commit-hash"], fields["host"]


def _same_toolchain(cargo, root, environment, cwd):
    """Refuse a test command whose Cargo, or the rustc that Cargo runs, isn't the toolchain
    Peeky builds with (PEEKY_RUST_CARGO and PEEKY_RUST_RUSTC, else rustup's pinned one): a
    build script or procedural macro that reads the compiler's version would build other code.
    The command's are asked as it runs them: its cargo, and the `rustc` on its PATH (RUSTC and
    `build.rustc` are refused apart), from the root, in its environment, so that rustup's
    proxies pick what a rust-toolchain.toml or a directory override names."""
    from peeky.cli import RUST_VERSION, _env_tool, _rustup
    from peeky.processes import without_credentials

    theirs = {"cargo": _which(cargo, environment, cwd), "rustc": _which("rustc", environment, cwd)}
    ours = {
        "cargo": _env_tool("PEEKY_RUST_CARGO") or _rustup("cargo"),
        "rustc": _env_tool("PEEKY_RUST_RUSTC") or _rustup("rustc"),
    }
    for tool in ("cargo", "rustc"):
        mine = ours[tool] and _toolchain_of(ours[tool], root, without_credentials(os.environ))
        if not mine:
            raise _unsupported(
                f"Peeky can't read its own {tool}'s version ({ours[tool] or 'none found'}): "
                f"install Rust {RUST_VERSION} (`rustup toolchain install {RUST_VERSION}`), or "
                f"set PEEKY_RUST_{tool.upper()}"
            )
        command = theirs[tool] and _toolchain_of(theirs[tool], root, environment)
        if not command:
            raise _unsupported(
                f"Peeky can't read the version of the {tool} the test command runs "
                f"({theirs[tool] or f'no {tool} on its PATH'}), so it can't tell that it is "
                f"Peeky's Rust {mine[0]}: run the test with that toolchain, or without --test"
            )
        if command != mine:
            shown = "Rust {} (commit {}, host {})".format
            raise _unsupported(
                f"The test command's {tool} is {shown(command[0], command[1][:9], command[2])}"
                f" ({theirs[tool]}, run from {root}), and Peeky builds with "
                f"{shown(mine[0], mine[1][:9], mine[2])} ({ours[tool]}): a build script or "
                "procedural macro that reads the compiler's version would build other code. "
                f"Make the command use Rust {mine[0]}: its cargo, the project's "
                "rust-toolchain.toml, or `rustup override`; or run without --test"
            )


def _host():
    """(the host triple, its `rustc --print cfg`) of Peeky's own rustc."""
    from peeky.cli import _env_tool, _rustup
    from peeky.processes import without_credentials

    rustc = _env_tool("PEEKY_RUST_RUSTC") or _rustup("rustc")
    try:
        if not rustc:
            raise OSError("no rustc found")
        env = without_credentials(os.environ)
        version, cfg = (
            subprocess.run([rustc, *arguments], capture_output=True, text=True, timeout=30, env=env)
            for arguments in (["-vV"], ["--print", "cfg"])
        )
        host = re.search(r"^host: (\S+)$", version.stdout, re.M)
        if host is None or cfg.returncode != 0:
            raise OSError(f"{rustc} names no host")
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise _unsupported(
            f"Peeky can't tell whether a runner in Cargo's configuration applies here ({exc}): "
            "take it out, or run without --test"
        ) from None
    return host[1], tuple(line.strip() for line in split_lines(cfg.stdout) if line.strip())


def _cargo_variables(environment, assignments, tables):
    """Notes for, or the refusal of, the variables of the test command's environment
    (``environment``: yours, with the command's ``assignments``) that change what Cargo
    builds or how it runs the test, as Peeky's Cargo runs in an environment of its own. A
    RUSTFLAGS of lints alone changes no code, and is noted, unless the configuration
    (``tables``) has `rustflags`, which Cargo would leave out in its place."""
    notes = []
    given = dict(assignments)
    for name in sorted(environment):
        value = environment[name]
        whose = "the command's" if name in given else "your environment's"
        drop = "drop it" if name in given else "unset it"
        if name in _FLAGS:
            flags = value.split("\x1f") if name == "CARGO_ENCODED_RUSTFLAGS" else value.split()
            configured = [
                str(path)
                for path, table, _ in tables
                if any("rustflags" in each for each in (_section(table, "build"), *_targets(table)))
            ]
            if _lints_only([flag for flag in flags if flag]) and not configured:
                notes.append(
                    f"Peeky builds with its own flags, without {whose} {name}: it sets only "
                    "lints, which change no code"
                )
                continue
            raise _unsupported(
                f"Cargo would build with {whose} {name} ({value!r}), and Peeky builds with its "
                f"own flags and the project's --cfg ones: {drop}, or run without --test"
                + (
                    f". With it, Cargo would leave out the `rustflags` of {configured[0]}, "
                    "which Peeky's build takes"
                    if configured
                    else ". Peeky's build takes the project's --cfg flags from its Cargo "
                    "configuration (build.rustflags in .cargo/config.toml)"
                )
            )
        if name in _RUSTC or (name.startswith(_CARGO_CHANGES) and name not in _CARGO_SAME):
            raise _unsupported(
                f"Cargo would build or run the test with {whose} {name}={value}, and Peeky "
                f"runs Cargo in an environment of its own, with its own toolchain: {drop}, or "
                "run without --test"
            )
    return notes


def _owner_of(manifests, scope, kind, name, sub):
    """The one package of ``scope`` with a ``kind`` target (`test`, `bin`) named ``name``, as
    Cargo finds it; refused where none or several have it."""
    if len(scope) == 1:
        # Cargo looks in that package alone; the build says if it has no such target.
        return scope[0]
    found = [p for p in scope if name in manifests[p]["tests" if kind == "test" else "bins"]]
    if len(found) == 1:
        return found[0]
    where = (
        f"`{scope[0]}`" if len(scope) == 1 else f"the packages Cargo selects ({', '.join(scope)})"
    )
    raise _unsupported(
        f"`cargo {sub} --{kind} {name}`: "
        + (
            f"no {kind} target {name} in {where}"
            if not found
            else f"{', '.join(found)} each have one"
        )
        + ": name the package with -p"
    )


def _packages(root):
    """{package name: {"directory", "features", "bins", "tests", "lib"}} of the workspace's
    own packages: the root manifest's, and its `[workspace] members`, read from their
    Cargo.toml (no Cargo run)."""
    from peeky.rust_workspace import _strings, member_candidates

    manifest = root / "Cargo.toml"
    if not manifest.is_file():
        raise _unsupported(
            f"{root} holds no Cargo.toml: give the workspace's directory as the target"
        )
    try:
        top = tomllib.loads(manifest.read_text())
    except (OSError, ValueError) as exc:
        raise _unsupported(f"Peeky can't read {manifest}: {exc}") from None
    section = top.get("workspace") if isinstance(top.get("workspace"), dict) else {}
    paths = [manifest] + [
        Path(path)
        for path in member_candidates(
            root, _strings(section, "members"), _strings(section, "exclude")
        )
    ]
    found, names = {}, {}
    for path in paths:
        try:
            data = top if path == manifest else tomllib.loads(path.read_text())
        except (OSError, ValueError):
            continue
        package = data.get("package")
        if not isinstance(package, dict) or not isinstance(package.get("name"), str):
            continue
        names[path.parent.resolve()] = package["name"]
        directory = path.parent
        # Cargo's target discovery: src/main.rs, src/bin/NAME.rs and src/bin/NAME/main.rs;
        # tests/NAME.rs and tests/NAME/main.rs; and the manifest's own [[bin]] and [[test]].
        bins = [b.get("name") for b in data.get("bin") or () if isinstance(b, dict)]
        if package.get("autobins", True):
            if (directory / "src/main.rs").is_file():
                bins.append(package["name"])
            bins += _discovered(directory / "src/bin")
        tests = [t.get("name") for t in data.get("test") or () if isinstance(t, dict)]
        if package.get("autotests", True):
            tests += _discovered(directory / "tests")
        features = sorted(k for k in data.get("features") or {} if k != "default")
        found.setdefault(
            package["name"],
            {
                "directory": directory,
                "features": features,
                "all_features": sorted({*features, *_optional_features(data)}),
                "bins": sorted({b for b in bins if b}),
                "tests": sorted({t for t in tests if t}),
                "lib": (directory / "src/lib.rs").is_file() or "lib" in data,
            },
        )
    defaults = _strings(section, "default-members")
    if defaults:
        chosen = [
            names[Path(p).parent.resolve()]
            for p in member_candidates(root, defaults)
            if Path(p).parent.resolve() in names
        ]
    elif isinstance(top.get("package"), dict) and top["package"].get("name") in found:
        chosen = [top["package"]["name"]]
    else:
        chosen = sorted(found)
    return found, chosen or sorted(found)


def _discovered(directory):
    """The targets Cargo discovers in ``directory``: `NAME.rs`, and `NAME/main.rs`."""
    if not directory.is_dir():
        return []
    return [p.stem for p in directory.glob("*.rs")] + [
        p.parent.name for p in directory.glob("*/main.rs")
    ]


def _optional_features(data):
    """The features a manifest's optional dependencies give, as `--all-features` turns them
    on: each one's name, unless a feature names it as `dep:name`."""
    tables = [data.get(key) for key in ("dependencies", "build-dependencies")]
    for target in (data.get("target") or {}).values():
        if isinstance(target, dict):
            tables += [target.get(key) for key in ("dependencies", "build-dependencies")]
    optional = {
        name
        for table in tables
        if isinstance(table, dict)
        for name, spec in table.items()
        if isinstance(spec, dict) and spec.get("optional") is True
    }
    hidden = {
        value[4:]
        for values in (data.get("features") or {}).values()
        if isinstance(values, list)
        for value in values
        if isinstance(value, str) and value.startswith("dep:")
    }
    return sorted(optional - hidden)


def _package_of(program, root, manifests):
    if program is None:
        raise _invalid("--test with a Cargo command needs --at FILE:LINE")
    path = (root / program).resolve()
    owners = [
        (len(info["directory"].parts), name)
        for name, info in manifests.items()
        if path.is_relative_to(info["directory"].resolve())
    ]
    if not owners:
        raise _unsupported(f"{program} is in no package of the workspace under {root}")
    return max(owners)[1]


def _default_binary(manifests, package):
    bins = manifests[package]["bins"]
    if len(bins) != 1:
        raise _unsupported(
            f"Package `{package}` has {len(bins)} binaries"
            + (f" ({', '.join(bins)})" if bins else "")
            + ": name one with --bin NAME, as `cargo run` needs"
        )
    return bins[0]


def _target_from_file(program, root, manifests, package):
    """The test target `cargo test` with none named would run the `--at` file in: the
    integration test that is the file, or the package's one test target. Else refused, with
    the package's test targets to choose from."""
    info = manifests[package]
    path = (root / program).resolve()
    tests = info["directory"].resolve() / "tests"
    if path.parent == tests and path.stem in info["tests"]:
        return "test", path.stem
    targets = ([("lib", None)] if info["lib"] else []) + [("test", t) for t in info["tests"]]
    if len(targets) == 1 and not info["bins"]:
        return targets[0]
    choices = ["--lib" if kind == "lib" else f"--test {name}" for kind, name in targets[:9]]
    raise _unsupported(
        f"`cargo test` without a target runs every test target of `{package}`, and Peeky runs "
        f"one: add the one that reaches {program}" + (f" ({', '.join(choices)})" if choices else "")
    )


def save_config(config, directory):
    """Save a configuration a test command made, by its content's hash, and return its path
    (the same configuration, the same file and the same build)."""
    text = json.dumps(config, sort_keys=True, indent=1) + "\n"
    path = Path(directory) / f"test-{hashlib.sha256(text.encode()).hexdigest()[:16]}.json"
    if not path.is_file() or path.read_text() != text:
        from peeky.cli import _private_write

        _private_write(path, text)
    return path
