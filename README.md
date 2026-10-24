# Peeky

**Let agents peek inside running programs.**

Peeky is a debugger that agents and people run from the command line. You or your agent, such as
Copilot, Claude or Codex, call it. It never calls a model. It supports Python, Java, C, C++ and
Rust, each experimentally ([Project status](#project-status)).

With Peeky you can:

- stop a program at a line and read its values;
- change a value, to test a hypothesis;
- try an edit on a fresh copy, and see what it changes;
- verify a fix with your own checker, in a fresh process.

Runtime experiments produce evidence. A separate verifier decides whether a source change passes
your checks.

## Install

Python 3.11 or later is required. Its `sqlite3` module must have SQLite 3.30 or later, with the
JSON functions, `json_each`, `json_tree` and window functions, for the evidence store.
`python3 -c 'import sqlite3; print(sqlite3.sqlite_version)'` prints the version. Before SQLite
3.41, `peeky sql` can use only the JSON and pragma table-valued functions
([COOKBOOK 4](docs/COOKBOOK.md#4-sql-over-saved-runs)). Run these, and every example, from the
repository root:

```sh
pip install -e .
```

For development and the tests, install the dev extras in a virtual environment:

```sh
python3 -m venv .venv
. .venv/bin/activate
pip install -e '.[dev]'
pytest tests/unit tests/integration
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
after-set: 1 scalar row equal to the hit's observed value left out (a number, flag or whole text; pointer, object and preview rows stay, as they may hide a change; `--json` has every row)
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
- Add `--hits N` or `--hits all` to capture repeated stops, `--arg` to pass program arguments, and
  `--json` for the full result.

Then check the fix with the program's checker:

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

## What to read next

| If you are | Read |
| --- | --- |
| An agent | [docs/AGENT_GUIDE.md](docs/AGENT_GUIDE.md), or install the [skills/peeky-debug](skills/peeky-debug/SKILL.md) skill |
| A person | the [COOKBOOK](docs/COOKBOOK.md): copy-paste recipes with real output |
| Debugging one language | its page: [Python](docs/lang/python.md), [Java](docs/lang/java.md), [C and C++](docs/lang/c-cpp.md), [Rust](docs/lang/rust.md) |
| Looking up an error code | [docs/ERRORS.md](docs/ERRORS.md) |
| Debugging a Java service or Gradle test, such as Venice's end-to-end tests | [docs/JAVA_ATTACH.md](docs/JAVA_ATTACH.md): attach to a `--debug-jvm` JVM |
| Running Peeky on another machine | [docs/REMOTE.md](docs/REMOTE.md) |

## The commands

| Command | What it does | Recipes |
| --- | --- | --- |
| `peeky describe TARGET` | Shows what you can ask: lines, names, and what `--set` can change. | [COOKBOOK 1](docs/COOKBOOK.md#1-first-look) |
| `peeky probe` | Stops, captures, changes and finishes, in one command. | [COOKBOOK 2](docs/COOKBOOK.md#2-asking-questions-with-probes) |
| `peeky queries`, `peeky sql` | Ask saved runs with canned queries, or in SQL. | [COOKBOOK 3 and 4](docs/COOKBOOK.md#3-comparing-runs) |
| `peeky session` | Several looks at one stop, with steps. | [COOKBOOK 5](docs/COOKBOOK.md#5-step-by-step-sessions) |
| `peeky verify` | Checks an edit with your checker, on a fresh build. | [COOKBOOK 6](docs/COOKBOOK.md#6-verifying-a-fix) |

Why Peeky is a tool and not an agent: [ADR 0005](docs/adr/0005-tool-not-agent.md).

## Ask follow-up questions with SQL

By default, every probe not refused before launch is saved with a `run_id`, and `--no-save` opts
out. The runs go to a local SQLite store, at `$PEEKY_STATE_DIR/evidence/evidence.db`. Capture
once, then ask as many questions as you like without running the program again, as heappy does for
heap dumps.

```sh
peeky probe app/ --at lib/parse.py:42 --run main.py --capture n,acc --hits 50 --label input=a
peeky sql "SELECT hit, value FROM last_run WHERE selector = 'acc' AND num > 100"
peeky queries --help                # canned questions and their --param names: diff [a] [b], set_effect [run], ...
peeky queries diff --param a=3 --param b=4   # first rows = first divergence between two runs
peeky sql --schema                  # a line per table and view; --schema captures (or all) in full
```

- The store is optional: a probe's output is already the answer.
- The store helps with large captures, where you query only what you need. It also helps to compare
  runs, and with slow or unrepeatable runs.
- A new question about the running program needs a new probe.
- For a separate database, add `--store PATH`.
- Queries are read-only, and bounded by `--limit` rows, 10 s and 16 MB per result. The newest 500
  runs are kept. [COOKBOOK 4](docs/COOKBOOK.md#4-sql-over-saved-runs) describes the tables.

## Run Peeky on another machine

The debugger runs in a worker, which a client calls over JSON HTTP. Start a worker with a secret
token:

```sh
export DEBUG_TOKEN="$(python3 -c 'import secrets; print(secrets.token_urlsafe(32))')"
peeky-worker --root "$PWD" --port 8765
```

In a second terminal, set the same secret and use the SDK:

```python
import os
from peeky.client import Client

client = Client("http://127.0.0.1:8765", os.environ["DEBUG_TOKEN"])
launch = client.call("launch", program="examples/pagination_bug.py", breakpoints=[{"line": 8}])
session = {"session_id": launch["session_id"]}
stop = client.call("wait", **session, timeout=2)
while stop["state"] == "running":
    stop = client.call("wait", **session, timeout=2)
if stop["state"] == "stopped":
    print(client.call("inspect", **session, stop_id=stop["stop_id"]))
print(client.call("report", **session))
client.call("close", **session)
```

A probe is one HTTP call ([ADR 0004](docs/adr/0004-stateless-probes.md)):

```python
Client(url, token).probe("pagination_bug.py:8", ["start"], {"start": "0"})
```

For remote use, run the worker on the target, and forward its loopback port over SSH:

```sh
ssh -N -L 8765:127.0.0.1:8765 user@target
peeky-agent --url http://127.0.0.1:8765 --model YOUR_MODEL --output runs/remote
```

- `peeky probe` and `peeky session start` take `--worker http://127.0.0.1:8765`, with the token in
  `PEEKY_WORKER_TOKEN` ([REMOTE.md](docs/REMOTE.md)).
- Keep the same source on the client and the worker. The runner checks the source's identity.
- Verification runs on the client, so its dependencies must match the target's environment.
- The SSH setup is documented. CI tests the network interface locally, not on a live cluster.

## Safety

- Peeky runs the code you give it. It is not a sandbox for hostile code, and not a production
  debugger service. [THREAT_MODEL.md](docs/THREAT_MODEL.md) has the trusted-code assumptions.
- The worker takes a secret token, and listens only on loopback (`127.0.0.1`). Reach it from
  another machine through SSH forwarding.
- `peeky-agent` sends selected source and runtime evidence to a model, and uses model credits.
  PR CI uses deterministic model fixtures and never calls a paid model.

## The optional harness: `peeky-agent`

`peeky-agent` is a reference harness that drives the same operations with Codex CLI's decisions.
It is not the main interface. To run it, install and sign in to Codex CLI, then choose a model your
account offers:

```sh
peeky-agent --model YOUR_MODEL --output runs/first-investigation
```

- The agent gets the bug report and the source of the bundled pagination program, which uses
  one-based page numbers wrongly.
- It chooses its own breakpoint and runtime change, and proposes a correction.
- The output directory has the decision and evidence report, the candidate source, and a diff
  after verification.
- The original source and the verifier's files are kept.

## Project status

This private repository is an MVP foundation. It was migrated from a working prototype, and
revised after two adversarial reviews.

- **Languages:** the minimum scope is Python, Java, C, C++ and Rust, delivered in that order in
  incremental releases. All five have experimental investigation loops, with independent candidate
  verification on narrow fixtures. None is general language support. See the
  [five-language plan](docs/LANGUAGES.md).
- **Python** runs through debugpy and the Debug Adapter Protocol (DAP).
- **Design notes per language:**
  - Java: the [worker](docs/WORKER.md#start-a-java-worker) and the [investigation loop](docs/AGENT_LOOP.md#java-paginationbugjava),
    with an example in `examples/java`;
  - C: the [worker](docs/WORKER.md) and the [investigation loop](docs/AGENT_LOOP.md#c-pagination_bugc);
  - C++: [how Peeky reads C and C++ values](docs/NATIVE_INTERNALS.md), the
    [worker](docs/WORKER.md#configure-a-c-target) and the
    [investigation loop](docs/AGENT_LOOP.md#c-pagination_bugcpp);
  - Rust: the [worker](docs/RUST_AGENT.md#the-cargo-build) and the [investigation loop](docs/AGENT_LOOP.md#rusts-two-gates).
- **The worker:** one controller and one session per worker, with session and stop identities. It
  offers local launch, breakpoints, bounded inspection, variable changes, continuation and evidence
  reports.
- **Later milestones:** Kubernetes deployment, multi-tenant workers, attach for languages other
  than Java, and a production mutation policy.
- **GPUs:** the [mixed-runtime direction](docs/MIXED_RUNTIME.md) covers future inference-engine
  debugging across Python, C++ and CUDA. It is planned, not current GPU support.

### CI and delivery

- CI covers Linux, with Python 3.11 and 3.13 and a separate Java 17 lane. macOS is used for local
  development, but isn't in the CI matrix yet.
- Required CI runs parallel quality, unit, integration and packaging lanes, each with a
  four-minute cutoff.
- The target is under five minutes from jobs starting to required checks completing. Hosted-runner
  queue time is measured separately.
- A cutoff is not evidence of speed. The measurements are in [TESTING.md](docs/TESTING.md).
- The current GitHub plan doesn't allow enforced branch protection for this private repository.
  CI runs, but merge gating must be checked by hand. TESTING.md has the API result.
- Delivery builds wheels and source archives as private GitHub Actions artifacts. There is no PyPI
  publishing, public release or deployment.

## Design documents

- [Design](docs/DESIGN.md): boundaries, lifecycle, protocol, failures and extension points.
- [Roadmap](docs/ROADMAP.md): the delivered slices with their acceptance criteria, what comes next
  and what stays deferred.
- [Testing and CI budget](docs/TESTING.md): parallel lanes and measured timings.
- [Runtime performance](docs/PERFORMANCE.md): a repeatable HTTP and debugger benchmark, with raw
  latency samples.
- [Code, logs and metrics](docs/adr/0002-correlated-evidence.md): the design for correlating
  evidence.
- [Stateless probes](docs/adr/0004-stateless-probes.md): one-request probes and the query
  interface, beside sessions.
- [Threat model](docs/THREAT_MODEL.md): trusted-code assumptions and remote constraints.
- [Adversarial review](docs/reviews/INITIAL_REVIEW.md): findings and dispositions.
- [Decision record](docs/adr/0001-mvp-boundaries.md): why Python, DAP and SSH came first.
- [Contributing](CONTRIBUTING.md): local checks and the change process.
