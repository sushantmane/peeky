# Peeky

**Let agents peek inside running programs.**

Peeky is a debugger that AI coding agents and people run from the command line. One command stops
a program at a line, reads its values, can change one to test a hypothesis, and runs the program
to its end. Another checks a fix with your own checker, on a fresh build. Peeky's rule: never show a
wrong or guessed value without a note. Where it can't prove a value, it withholds it and says why.
It supports Python, Java, C, C++ and Rust, all experimental. `peeky` never calls a model. Your
agent, such as Copilot, Claude or Codex, calls Peeky, and so can you.

> **Status:** experimental. Version 0.1.0 is tagged. Peeky isn't on PyPI yet. See Project status,
> below.

## Install

Peeky needs Python 3.11 or later. Install it as a tool, in an environment of its own, with
[pipx][pipx] or [uv][uv]. Peeky isn't on PyPI yet, so for now install it from a clone of the
repository:

```sh
git clone https://github.com/sushantmane/peeky.git && cd peeky
pipx install .    # or: uv tool install .
peeky --version
```

`peeky --version` prints `peeky 0.1.0`. Run the examples below from the repository root. Once
Peeky is on PyPI, `pipx install peeky-debug` or `uv tool install peeky-debug` installs it.

An environment of its own keeps Peeky and your other packages apart. Peeky needs debugpy 1.8.22
exactly, the version its Python support is verified with. Another package on PyPI also has a
module named `peeky` (see The package name, below).

- **Python programs** run on their project's interpreter, so they import what the project
  installs. That is the root's `.venv` or `venv`, else the active virtual environment, else
  Peeky's own. `--python PATH` names another, and the output says which one ran. It must be
  CPython 3.11 or later. The project needs no debugpy of its own ([Python][python]).
- **macOS:** the built-in `python3` is 3.9, too old for Peeky. There, `pip install` fails with
  `No matching distribution found for debugpy==1.8.22`, which doesn't name the cause. Install
  Python 3.11 or later, from python.org or Homebrew, and name it:
  `pipx install --python python3.11 .` or `uv tool install --python 3.11 .`.
- **SQLite:** the evidence store needs SQLite 3.30 or later, with its JSON functions.
  `python3 -c 'import sqlite3; print(sqlite3.sqlite_version)'` prints the version.
- **Other languages:** Python needs nothing more. Java needs JDK 17 or later, and its debugger
  adapter, which `peeky setup-java` builds once. C, C++ and Rust need LLVM's `lldb-dap`.
  [COOKBOOK 0][cookbook-0] lists what to install.

### The package name

On PyPI the package will be `peeky-debug`, because `peeky` there is an
[unrelated project][pypi-peeky]. Peeky's commands (`peeky`, `peeky-worker`, `peeky-probe`,
`peeky-agent`) and its Python module (`peeky`) keep the name Peeky.

That project installs a module and a command named `peeky` too. Installed in one environment, the
two overwrite each other's files, and one or both break. Uninstalling either one removes files the
other needs. So install Peeky with pipx or uv, which give each tool an environment of its own. The
two `peeky` commands still share one name on your `PATH`.

### From a release

A [GitHub release][releases] has a wheel, `peeky_debug-VERSION-py3-none-any.whl`, and a source
archive, `peeky_debug-VERSION.tar.gz`. v0.1.0 came before the package's rename, so its files are
`peeky-0.1.0-py3-none-any.whl` and `peeky-0.1.0.tar.gz`. Install the wheel as a tool, with
pipx, or with `uv tool install` and the same path:

```sh
pipx install ./peeky_debug-VERSION-py3-none-any.whl
```

The wheel has the commands, and what `peeky setup-java` builds the Java adapter from. It doesn't
have the examples. They are in the source archive. Unpack it, and run every example from its top
directory:

```sh
tar -xzf peeky_debug-VERSION.tar.gz && cd peeky_debug-VERSION
```

## A first probe

Reading the code, a print and the project's tests fix most bugs, so start there. Peeky pays off
when a print costs more ([when][when]):

- the value is deep in the project's code, and a print means an edit and maybe a rebuild;
- you need it at many hits, or across runs;
- you want to change a value mid-run to test a hypothesis, with no edit;
- builds or test runs are slow, and one probe reads many values in one run;
- you want to check a fix on a fresh copy, with your files unchanged.

On your own project, the first command depends on its kind ([first command][first-command]):

- **Python:** from the repository's root,
  `peeky probe . --at src/pkg/mod.py:LINE --capture a,b --run script.py`. The script reaches the
  line; for a pytest test, a two-line one calls `pytest.main`. A project installed without `-e`
  runs its `site-packages` copy, not its source.
- **Java:** attach to a Maven or Gradle test started waiting for a debugger, or launch a class
  with no Maven or Gradle dependencies.
- **C and C++:** a directory of sources, which Peeky builds itself. A project with its own build,
  such as Make or CMake, isn't supported yet.
- **Rust:** a binary crate with no dependencies. A crate with dependencies isn't supported yet.

`examples/pagination_bug.py` has a bug: page 1 starts at the second page. Stop at line 8, read two
values, and set `start` to 0:

```sh
peeky probe examples/pagination_bug.py --at 8 --capture 'start,items[1]' --set start=0
```

```text
hit  phase      line  function  selector  type  value
1    observed   8     get_page  start     int   3
1    observed   8     get_page  items[1]  int   20
1    after-set  8     get_page  start     int   0
after-set: 1 number, flag, null or text row equal to the observed value left out; `--json` has all
set start: 3 -> 0
status: completed  hits: 1  seconds: 1.11  max pause: 18.4 ms
output:
  PAGE: [10, 20, 30]
evidence: saved as run 1; query it with `peeky queries run --param run=1` or `peeky sql` (see `peeky sql --schema`)
```

- One command does it all, with no session to manage. Peeky starts a fresh run, stops at the
  line, captures the values and makes the change. C, C++, Rust and Java targets are built first, in
  a fresh workspace. Then it runs the program to its end, prints the table
  and cleans up.
- `set start: 3 -> 0` and `PAGE: [10, 20, 30]` show what the change did. That is evidence about
  this run, not a verified fix.
- Selectors are dotted paths, indexes and string keys, such as `request.page`, `args[0]` and
  `order['id']`. Peeky never evaluates them as expressions.
- The same command works for C and C++ programs, Rust crates and Java (with `--config`).

Then check a fix with the program's checker:

```sh
peeky verify examples/pagination_bug.py --check examples/verify_pagination.py \
  --old 'page * page_size' --new '(page - 1) * page_size'
```

```text
passed: True  stage: verification  seconds: 0.038
checker exit: 0
output:
PASS: 12 independent pagination checks
...
```

`verify` edits a fresh copy, so your source is left as it was.

## The commands

| Command | What it does | Recipes |
| --- | --- | --- |
| `peeky describe TARGET` | Shows what you can ask: lines, names, and what `--set` can change. | [COOKBOOK 1][cookbook-1] |
| `peeky probe` | Stops, captures, changes and finishes, in one command. | [COOKBOOK 2][cookbook-2] |
| `peeky queries`, `peeky sql` | Ask saved runs with canned queries, or in SQL. | [COOKBOOK 3 and 4][cookbook-3] |
| `peeky session` | Several looks at one stop, with steps. | [COOKBOOK 5][cookbook-5] |
| `peeky verify` | Checks an edit with your checker, or a Python project's tests, on a fresh copy. | [COOKBOOK 6][cookbook-6] |

- Every probe not refused before launch is saved in a local SQLite store, so you can ask
  follow-up questions without running the program again. `--no-save` opts out.
- `peeky-worker` runs the debugger on another machine, reached through an SSH tunnel
  ([REMOTE.md][remote]).
- Why Peeky is a tool and not an agent: [ADR 0005][adr-0005].

## What to read next

| If you are | Read |
| --- | --- |
| An agent | [docs/AGENT_GUIDE.md][agent-guide], or install the [skills/peeky-debug][skill] skill |
| A person | the [COOKBOOK][cookbook]: copy-paste recipes with real output |
| Debugging one language | its page: [Python][python], [Java][java], [C and C++][c-cpp], [Rust][rust] |
| Looking up an error code | [docs/ERRORS.md][errors] |
| Debugging a JVM started by Gradle with `--debug-jvm` | [docs/JAVA_ATTACH.md][java-attach] |
| Running Peeky on another machine | [docs/REMOTE.md][remote] and [docs/WORKER.md][worker] |
| Curious how it got here | [docs/HISTORY.md][history] and the [CHANGELOG][changelog] |

## Safety

- Peeky runs the code you give it. It is not a sandbox for hostile code, and not a production
  debugger service. [THREAT_MODEL.md][threat-model] has the trusted-code assumptions.
- The worker takes a secret token, and listens only on loopback (`127.0.0.1`). Reach it from
  another machine through SSH forwarding.
- The worker reads its token from `DEBUG_TOKEN`. Every client, `peeky-agent --url` too, reads it
  from `PEEKY_WORKER_TOKEN`.
- Start that tunnel with `ssh -N -o ExitOnForwardFailure=yes -L 8765:127.0.0.1:8765 user@host`.
  Without the option, ssh only warns when another process holds the local port, and the token goes
  to that process.
- Peeky itself sends nothing to a model. The optional `peeky-agent`, below, does.
- To report a vulnerability privately, see [SECURITY.md][security].

## The optional harness: `peeky-agent`

`peeky-agent` is an experimental reference harness. It drives the same operations with a model's
decisions, through Codex CLI. It is not the main interface.

- It sends selected source and runtime evidence to the model, and uses model credits.
- To run it, install and sign in to Codex CLI, then pick a model your account offers:
  `peeky-agent --model YOUR_MODEL --output runs/first-investigation`.
- CI uses deterministic decision fixtures, and never calls a model.
- [docs/AGENT_LOOP.md][agent-loop] describes the loop.

`peeky-agent` runs code the model writes, without a sandbox:

- The controller applies the model's `finish` edit to a copy of the source, then runs that copy
  with the checker. Both run with your permissions.
- The model reads the source, the bug report, and the program's output and values. Treat all of it
  as untrusted: text in it can steer the model (prompt injection) into writing code that then runs.
- Run `peeky-agent` only on code and inputs you trust, or inside a container or VM.

## Project status

- **Version:** 0.1.0, tagged on 2026-10-26, with a GitHub release. The
  [CHANGELOG][changelog] lists what it covers, its known gaps and what changed since.
- **Release:** a public release, and a package on PyPI, are pending.
- **Languages:** Python, Java, C, C++ and Rust are all experimental. Each is qualified on narrow
  fixtures, and rounds of live agent evaluation shaped them
  ([HISTORY.md][evaluations]). None is general language support.
- **Also experimental:** Java attach, remote workers and `peeky-agent`.
- **Not supported:** CUDA ([planned][cuda]), Kubernetes workers, several users
  per worker, and attach for languages other than Java.
- **CI:** Linux only, with Python 3.11 and 3.13, Java 17, C, C++ (clang and GCC), Rust, a remote
  SSH lane and a wheel smoke test. macOS is used for development, but isn't in the CI matrix.
- **Hosted CI** on GitHub Actions is paused, and resumes when the repository is public. Until then,
  each change is checked by the Docker mirror, `scripts/ci_linux`, with the results in the pull
  request. It runs the same checks and test suites as CI's jobs, once each. It skips remote SSH
  e2e, runs the integration tests on one Python, and adds lanes hosted CI doesn't have
  ([TESTING.md][docker-mirror]).
- **Licence:** Apache License 2.0 (see Licence, below).

## Documentation

- [Design][design]: boundaries, lifecycle, protocol, failures and extension points.
- [Roadmap][roadmap]: what is delivered, what a language needs before Peeky claims it, what
  comes next and what stays deferred.
- [Testing][testing]: CI lanes, the Docker mirror and local test commands.
- [Performance][performance]: a repeatable worker benchmark, and dated measurements.
- [Decision records][adr]: the architecture decisions, and why they were made.
- [History][history]: milestones and live agent evaluation reports.
- [Presentation][presentation]: why Peeky exists, what it is and how it works, for
  executives and engineers. The [PowerPoint][powerpoint] opens in Keynote too.

## Contributing

[CONTRIBUTING.md][contributing] has the local checks and the change process. Everyone taking
part follows the [code of conduct][code-of-conduct]. Report bugs and ask for features with the
[issue templates](https://github.com/sushantmane/peeky/issues/new/choose). Report vulnerabilities
privately instead, as [SECURITY.md][security] describes.

## Licence

Apache License 2.0: see [LICENSE][license] and [NOTICE][notice]. The Java adapter that
`peeky setup-java` builds locally is Microsoft java-debug, under the Eclipse Public License 1.0
([notices][java-notices]).

<!-- Full URLs: PyPI shows this README, where relative links break. -->
[pipx]: https://pipx.pypa.io/stable/
[uv]: https://docs.astral.sh/uv/
[pypi-peeky]: https://pypi.org/project/peeky/
[releases]: https://github.com/sushantmane/peeky/releases
[cookbook-0]: https://github.com/sushantmane/peeky/blob/main/docs/COOKBOOK.md#what-each-language-also-needs
[cookbook-1]: https://github.com/sushantmane/peeky/blob/main/docs/COOKBOOK.md#1-first-look
[cookbook-2]: https://github.com/sushantmane/peeky/blob/main/docs/COOKBOOK.md#2-asking-questions-with-probes
[cookbook-3]: https://github.com/sushantmane/peeky/blob/main/docs/COOKBOOK.md#3-comparing-runs
[cookbook-5]: https://github.com/sushantmane/peeky/blob/main/docs/COOKBOOK.md#5-step-by-step-sessions
[cookbook-6]: https://github.com/sushantmane/peeky/blob/main/docs/COOKBOOK.md#6-verifying-a-fix
[cookbook]: https://github.com/sushantmane/peeky/blob/main/docs/COOKBOOK.md
[remote]: https://github.com/sushantmane/peeky/blob/main/docs/REMOTE.md
[adr-0005]: https://github.com/sushantmane/peeky/blob/main/docs/adr/0005-tool-not-agent.md
[agent-guide]: https://github.com/sushantmane/peeky/blob/main/docs/AGENT_GUIDE.md
[when]: https://github.com/sushantmane/peeky/blob/main/docs/AGENT_GUIDE.md#when-peeky-beats-a-print
[first-command]: https://github.com/sushantmane/peeky/blob/main/docs/AGENT_GUIDE.md#your-first-command
[skill]: https://github.com/sushantmane/peeky/blob/main/skills/peeky-debug/SKILL.md
[python]: https://github.com/sushantmane/peeky/blob/main/docs/lang/python.md
[java]: https://github.com/sushantmane/peeky/blob/main/docs/lang/java.md
[c-cpp]: https://github.com/sushantmane/peeky/blob/main/docs/lang/c-cpp.md
[rust]: https://github.com/sushantmane/peeky/blob/main/docs/lang/rust.md
[errors]: https://github.com/sushantmane/peeky/blob/main/docs/ERRORS.md
[java-attach]: https://github.com/sushantmane/peeky/blob/main/docs/JAVA_ATTACH.md
[worker]: https://github.com/sushantmane/peeky/blob/main/docs/WORKER.md
[history]: https://github.com/sushantmane/peeky/blob/main/docs/HISTORY.md
[evaluations]: https://github.com/sushantmane/peeky/blob/main/docs/HISTORY.md#live-agent-evaluations
[changelog]: https://github.com/sushantmane/peeky/blob/main/CHANGELOG.md
[threat-model]: https://github.com/sushantmane/peeky/blob/main/docs/THREAT_MODEL.md
[security]: https://github.com/sushantmane/peeky/blob/main/SECURITY.md
[agent-loop]: https://github.com/sushantmane/peeky/blob/main/docs/AGENT_LOOP.md
[cuda]: https://github.com/sushantmane/peeky/blob/main/docs/ROADMAP.md#cuda-61
[docker-mirror]: https://github.com/sushantmane/peeky/blob/main/docs/TESTING.md#the-docker-mirror
[design]: https://github.com/sushantmane/peeky/blob/main/docs/DESIGN.md
[roadmap]: https://github.com/sushantmane/peeky/blob/main/docs/ROADMAP.md
[testing]: https://github.com/sushantmane/peeky/blob/main/docs/TESTING.md
[performance]: https://github.com/sushantmane/peeky/blob/main/docs/PERFORMANCE.md
[adr]: https://github.com/sushantmane/peeky/tree/main/docs/adr
[presentation]: https://github.com/sushantmane/peeky/blob/main/docs/presentation/Peeky.pdf
[powerpoint]: https://github.com/sushantmane/peeky/blob/main/docs/presentation/Peeky.pptx
[contributing]: https://github.com/sushantmane/peeky/blob/main/CONTRIBUTING.md
[code-of-conduct]: https://github.com/sushantmane/peeky/blob/main/CODE_OF_CONDUCT.md
[license]: https://github.com/sushantmane/peeky/blob/main/LICENSE
[notice]: https://github.com/sushantmane/peeky/blob/main/NOTICE
[java-notices]: https://github.com/sushantmane/peeky/blob/main/src/peeky/java_adapter/NOTICES.md
