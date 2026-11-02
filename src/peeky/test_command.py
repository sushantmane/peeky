"""`--test`: the project's own test command names what Peeky runs (ADR 0009).

The words after `--` are the command that runs the failing test, as typed in the directory it
runs in. Its first word decides the kind:

- Python: `pytest`, `python -m pytest`, or a script, on the interpreter that command runs.
- C and C++: a binary inside the target directory (`--binary`), or the one test `ctest` names.
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
    if test.kind in ("binary", "rust") and test.environment:
        test = _own_environment(test)
    return test


_LINT_OPTIONS = frozenset({"-A", "-W", "-D", "-F", "--cap-lints", "--force-warn"})
_LINT_WORD = re.compile(r"-[AWDF]\S+|--(?:cap-lints|force-warn)=\S+")


def _lints_only(flags):
    """Whether rustc ``flags`` set only lints (`-Awarnings`, `--cap-lints warn`)."""
    words = flags.split()
    while words:
        word = words.pop(0)
        if word in _LINT_OPTIONS:
            if not words:
                return False
            words.pop(0)
        elif not _LINT_WORD.fullmatch(word):
            return False
    return True


def _own_environment(test):
    """A C, C++ or Rust test command's environment assignments: Peeky runs the binary, and
    Cargo, in an environment of its own (build.INHERITED_ENV), so others never reach them.
    A RUSTFLAGS of lints alone changes no code, and is noted; another assignment is refused."""
    from dataclasses import replace

    from peeky.build import INHERITED_ENV

    notes = list(test.notes)
    for name, value in test.environment:
        if name in INHERITED_ENV:
            continue
        flags = test.kind == "rust" and name in ("RUSTFLAGS", "CARGO_BUILD_RUSTFLAGS")
        if flags and _lints_only(value):
            notes.append(
                f"Peeky builds with its own flags, without the command's {name}: it sets only "
                "lints, which change no code"
            )
            continue
        what = "Cargo and the test" if test.kind == "rust" else "the binary"
        raise _unsupported(
            f"Peeky runs {what} in an environment of its own, so {name}=… in the test "
            "command wouldn't reach "
            + ("them" if test.kind == "rust" else "it")
            + ": drop it"
            + (
                ". Peeky's build takes the project's --cfg flags from its Cargo configuration "
                "(build.rustflags in .cargo/config.toml)"
                if flags
                else ""
            )
        )
    return replace(test, notes=tuple(notes))


def _plan(words, root, program, cwd):
    cwd = Path(cwd or os.getcwd())
    root = Path(root).resolve()
    assignments, words = split_environment(words)
    if not words:
        raise _invalid(
            "--test takes the command that runs the failing test after `--`: "
            "peeky probe . --at FILE:LINE --test -- pytest tests/test_x.py::test_y"
        )
    environment = {**os.environ, **dict(assignments)}
    first = words[0]
    name = Path(first).name
    if name in _SHELLS or any(word in ("&&", "||", ";", "|") for word in words):
        raise _unsupported(
            f"--test runs one command, not a shell line ({shlex.join(words)}): give the "
            "command that runs the test itself, such as `pytest tests/test_x.py::test_y`"
        )
    if name in _JAVA_TOOLS:
        return TestCommand("java", words, assignments)
    if name == "cargo":
        return _cargo(words, assignments, root, program, environment, cwd)
    if name == "ctest":
        return _ctest(words, assignments, root, environment, cwd)
    if name in ("pytest", "py.test") or _PYTHON.fullmatch(name):
        return _python(words, assignments, root, environment, cwd, program)
    if name in _OTHER_TOOLS:
        raise _unsupported(
            f"--test can't run `{name}`: {_OTHER_TOOLS[name]}. Give the command that runs "
            "the one failing test: its binary with its arguments (C, C++), `pytest …`, "
            "`cargo test …`, or the `java` command it runs"
        )
    path = _which(first, environment, cwd)
    if path is None or not path.is_file():
        raise _invalid(f"--test's command `{first}` is not a file, nor on PATH")
    from peeky.cli import _script_python

    if _script_python(path, environment) is not None:
        return _python(words, assignments, root, environment, cwd, program)
    if not _inside(path, root):
        raise _unsupported(
            f"--test's program {path} is outside the target directory {root}: Peeky debugs a "
            "binary built from the sources under it. Give the target directory that holds it"
        )
    return _binary(words, assignments, path, root, cwd)


def _python(words, assignments, root, environment, cwd, program=None):
    from peeky.cli import _script_python

    first = words[0]
    path = _which(first, environment, cwd)
    if path is None or not path.is_file():
        raise _invalid(f"--test's command `{first}` is not a file, nor on PATH")
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
                    "pytest as a module and no other. Run the module from a script "
                    "(run.py: `import runpy, sys; sys.argv = ['x', *ARGS]; "
                    f"runpy.run_module('{module}', run_name='__main__', alter_sys=True)`), "
                    "and give `--test -- python run.py`"
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
        if path.name in ("pytest", "py.test"):
            _same_directory(cwd, root, "pytest")
            return TestCommand(
                "python",
                words,
                assignments,
                python=python,
                module="pytest",
                arguments=tuple(words[1:]),
                notes=_root_first(root, program),
            )
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


def _root_first(root, program):
    """A note where `pytest` and `python -m pytest` may import different code: the latter,
    which Peeky runs, puts the root first on `sys.path`, so a package at the root (the
    `--at` file's) is imported from there, where the `pytest` script finds an installed
    copy, if one isn't installed editable."""
    parts = Path(program or "").parts
    if len(parts) > 1 and (Path(root) / parts[0] / "__init__.py").is_file():
        return (
            f"Peeky runs pytest as `python -m pytest`, which imports `{parts[0]}` from the "
            "root; the `pytest` script imports an installed copy instead, unless it is "
            "installed editable (`pip install -e .`)",
        )
    return ()


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


def _ctest(words, assignments, root, environment, cwd):
    """The one test `ctest`'s own words select, from `ctest --show-only=json-v1`."""
    for word in words[1:]:
        if word in ("-N", "--show-only") or word.startswith("--show-only="):
            raise _invalid("Give --test the ctest command that runs the test, without -N")
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
    command = [str(word) for word in test.get("command") or ()]
    if not command:
        raise _unsupported(f"ctest names no command for test {test.get('name')}")
    path = Path(command[0])
    if not path.is_file() or not _inside(path, root):
        raise _unsupported(
            f"ctest's test {test.get('name')} runs {command[0]}, which isn't a file in the "
            f"target directory {root}"
        )
    properties = {p.get("name"): p.get("value") for p in test.get("properties") or ()}
    notes = []
    where = properties.get("WORKING_DIRECTORY")
    if not where or os.path.realpath(where) != os.path.realpath(root):
        notes.append(
            f"ctest runs {test.get('name')} in {where or 'the build directory that defines it'}; "
            "Peeky runs it in the target directory, so a relative path it reads may name "
            "another file"
        )
    if environment_property := properties.get("ENVIRONMENT"):
        notes.append(
            f"ctest sets {environment_property} for {test.get('name')}; Peeky runs the binary "
            "in an environment of its own, without it, so the test may run otherwise"
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
    return TestCommand(
        "rust",
        tuple(words),
        assignments,
        config=config,
        arguments=(*filters, *harness),
        notes=tuple(notes),
    )


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
