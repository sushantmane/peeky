# Peeky

**Let agents peek inside running programs.**

Peeky is a debugger that AI coding agents and people run from the command line. One command stops
a program at a line, reads its values, can change one to test a hypothesis, and runs the program
to its end. Another checks a fix with your own checker, on a fresh build. Peeky never shows a
wrong or guessed value without a note: where it can't prove a value, it withholds it and says why.
It supports Python, Java, C, C++ and Rust, all experimental. Peeky never calls a model. Your
agent, such as Copilot, Claude or Codex, calls Peeky, and so can you.

> **Status:** experimental. Version 0.1.0 is tagged. Peeky isn't on PyPI yet. See
> [Project status](#project-status).

## Install

Peeky needs Python 3.11 or later. Install it from the repository:

```sh
git clone https://github.com/sushantmane/peeky.git && cd peeky
python3.11 -m venv .venv && . .venv/bin/activate
pip install -e .
peeky --version
```

`peeky --version` prints `peeky 0.1.0`. Run the examples below from the repository root.

- **macOS:** the built-in `python3` is 3.9. There, `pip install` fails with
  `No matching distribution found for debugpy==1.8.22`, which doesn't name the cause. Install
  Python 3.11 or later, from python.org or Homebrew, and make the virtual environment with it.
- **SQLite:** the evidence store needs SQLite 3.30 or later, with its JSON functions.
  `python3 -c 'import sqlite3; print(sqlite3.sqlite_version)'` prints the version.
- **Other languages:** Python needs nothing more. Java needs JDK 17 and an adapter bundle that a
  script builds. C, C++ and Rust need LLVM's `lldb-dap`.
  [COOKBOOK 0](docs/COOKBOOK.md#what-each-language-also-needs) lists what to install.

### From a release

The [v0.1.0 release](https://github.com/sushantmane/peeky/releases/tag/v0.1.0) has a wheel,
`peeky-0.1.0-py3-none-any.whl`, and a source archive, `peeky-0.1.0.tar.gz`:

```sh
python3.11 -m venv .venv && . .venv/bin/activate
pip install peeky-0.1.0-py3-none-any.whl
```

The wheel has the commands, but not the examples. They are in the source archive. Unpack it, and
run every example from its top directory:

```sh
tar -xzf peeky-0.1.0.tar.gz && cd peeky-0.1.0
```

## A first probe

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

- One command does it all, with no session to manage. Peeky builds fresh, stops at the line,
  captures the values and makes the change. Then it runs the program to its end, prints the table
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
| `peeky describe TARGET` | Shows what you can ask: lines, names, and what `--set` can change. | [COOKBOOK 1](docs/COOKBOOK.md#1-first-look) |
| `peeky probe` | Stops, captures, changes and finishes, in one command. | [COOKBOOK 2](docs/COOKBOOK.md#2-asking-questions-with-probes) |
| `peeky queries`, `peeky sql` | Ask saved runs with canned queries, or in SQL. | [COOKBOOK 3 and 4](docs/COOKBOOK.md#3-comparing-runs) |
| `peeky session` | Several looks at one stop, with steps. | [COOKBOOK 5](docs/COOKBOOK.md#5-step-by-step-sessions) |
| `peeky verify` | Checks an edit with your checker, on a fresh build. | [COOKBOOK 6](docs/COOKBOOK.md#6-verifying-a-fix) |

- Every probe not refused before launch is saved in a local SQLite store, so you can ask
  follow-up questions without running the program again. `--no-save` opts out.
- `peeky-worker` runs the debugger on another machine, reached through an SSH tunnel
  ([REMOTE.md](docs/REMOTE.md)).
- Why Peeky is a tool and not an agent: [ADR 0005](docs/adr/0005-tool-not-agent.md).

## What to read next

| If you are | Read |
| --- | --- |
| An agent | [docs/AGENT_GUIDE.md](docs/AGENT_GUIDE.md), or install the [skills/peeky-debug](skills/peeky-debug/SKILL.md) skill |
| A person | the [COOKBOOK](docs/COOKBOOK.md): copy-paste recipes with real output |
| Debugging one language | its page: [Python](docs/lang/python.md), [Java](docs/lang/java.md), [C and C++](docs/lang/c-cpp.md), [Rust](docs/lang/rust.md) |
| Looking up an error code | [docs/ERRORS.md](docs/ERRORS.md) |
| Debugging a JVM started by Gradle with `--debug-jvm` | [docs/JAVA_ATTACH.md](docs/JAVA_ATTACH.md) |
| Running Peeky on another machine | [docs/REMOTE.md](docs/REMOTE.md) and [docs/WORKER.md](docs/WORKER.md) |
| Curious how it got here | [docs/HISTORY.md](docs/HISTORY.md) and the [CHANGELOG](CHANGELOG.md) |

## Safety

- Peeky runs the code you give it. It is not a sandbox for hostile code, and not a production
  debugger service. [THREAT_MODEL.md](docs/THREAT_MODEL.md) has the trusted-code assumptions.
- The worker takes a secret token, and listens only on loopback (`127.0.0.1`). Reach it from
  another machine through SSH forwarding.
- Peeky itself sends nothing to a model. The optional `peeky-agent`, below, does.

## The optional harness: `peeky-agent`

`peeky-agent` is an experimental reference harness. It drives the same operations with a model's
decisions, through Codex CLI. It is not the main interface.

- It sends selected source and runtime evidence to the model, and uses model credits.
- To run it, install and sign in to Codex CLI, then pick a model your account offers:
  `peeky-agent --model YOUR_MODEL --output runs/first-investigation`.
- CI uses deterministic decision fixtures, and never calls a model.
- [docs/AGENT_LOOP.md](docs/AGENT_LOOP.md) describes the loop.

## Project status

- **Version:** 0.1.0, tagged on 2026-10-26, with a GitHub release. The
  [CHANGELOG](CHANGELOG.md) lists what it covers, its known gaps and what changed since.
- **Release:** a public release, and a package on PyPI, are pending.
- **Languages:** Python, Java, C, C++ and Rust are all experimental. Each is qualified on narrow
  fixtures, and twelve rounds of live agent evaluation shaped them
  ([HISTORY.md](docs/HISTORY.md#live-agent-evaluations)). None is general language support.
- **Also experimental:** Java attach, remote workers and `peeky-agent`.
- **Not supported:** CUDA ([planned](docs/ROADMAP.md#cuda-61)), Kubernetes workers, several users
  per worker, and attach for languages other than Java.
- **CI:** Linux only, with Python 3.11 and 3.13, Java 17, C, C++ (clang and GCC), Rust, a remote
  SSH lane and a wheel smoke test. macOS is used for development, but isn't in the CI matrix.
- **Hosted CI** on GitHub Actions is paused, and resumes when the repository is public. Until then,
  each change is checked by the same Linux lanes in Docker (`scripts/ci_linux/run_lanes.sh`), with
  the results in the pull request.
- **Licence:** none has been chosen yet, so none is included.

## Documentation

- [Design](docs/DESIGN.md): boundaries, lifecycle, protocol, failures and extension points.
- [Roadmap](docs/ROADMAP.md): what is delivered, what comes next and what stays deferred.
- [Languages](docs/LANGUAGES.md): the five-language plan and its acceptance gates.
- [Testing](docs/TESTING.md): CI lanes and measured timings.
- [Performance](docs/PERFORMANCE.md): a repeatable HTTP and debugger benchmark.
- [Decision records](docs/adr/) and [reviews](docs/reviews/), with each finding's disposition.
- [History](docs/HISTORY.md): milestones, evaluation reports and reviews.

## Contributing

[CONTRIBUTING.md](CONTRIBUTING.md) has the local checks and the change process. Everyone taking
part follows the [code of conduct](CODE_OF_CONDUCT.md). Report bugs and ask for features with the
[issue templates](https://github.com/sushantmane/peeky/issues/new/choose).
