# History

Peeky's milestones, by date. The [CHANGELOG](../CHANGELOG.md) says what each version holds. Each
change's details are in its pull request on GitHub.

## Milestones

### 2026-10-14: the first slices

- The Python bootstrap: debugpy, a worker with session and stop identities, verification in a
  fresh process, and parallel CI.
- The [five-language plan](LANGUAGES.md): Python first, then Java, C, C++ and Rust.
- Java through java-debug, run headless, with an HTTP worker and verified repairs.
- C through LLDB, read-only C++ inspection, and a first Rust worker.

### 2026-10-15: one command-line tool

- The `peeky` command: `describe`, `probe`, `session` and `verify`. Peeky became a tool that
  agents call ([ADR 0005](adr/0005-tool-not-agent.md)).
- Java attach, error codes, a ten-bug corpus and short pauses for agents
  ([ADR 0006](adr/0006-agent-speed.md)).
- A real program: CPython 3.9.6's `ipaddress`, with the bug behind CVE-2024-4032.
- Optimized builds, with locals the debugger can't read reported as such.
- Remote workers over SSH tunnels ([REMOTE.md](REMOTE.md)).
- The first live agent evaluations (below).

### 2026-10-16: the evidence store and a whole-code review

- `peeky sql` and `peeky queries`, over every saved run ([ADR 0004](adr/0004-stateless-probes.md)).
- A [whole-code review](reviews/WHOLE_CODE_REVIEW_2026-10-16.md), with every finding fixed.
- Remote workers tested on a second host: a Linux container, reached over SSH.
- The [COOKBOOK](COOKBOOK.md), and stepping in sessions.

### 2026-10-17 to 2026-10-21: evaluation rounds and their fixes

- Candidate fixes in probes (`probe --old/--new`), and several edits per `verify`.
- A plan for CUDA, with no GPU support claimed ([ADR 0007](adr/0007-cuda-gdb-dap.md)).
- Java objects and lists, Rust containers and structs, and Python caller frames.
- Changes at chosen lines and hits (`--set-at`), return values, and text set in place.
- Evaluation rounds three to nine, each followed by fixes.

### 2026-10-22: plain English, and every language on a remote host

- Every page, and the longest messages, rewritten in plain English.
- A page and a cookbook per language ([Python](lang/python.md), [Java](lang/java.md),
  [C and C++](lang/c-cpp.md), [Rust](lang/rust.md)).
- Java, C, C++ and Rust qualified on a separate host.

### 2026-10-23 to 2026-10-25: GCC, and the last rounds before 0.1.0

- C++ built by GCC, with what LLDB misreads withheld.
- The evidence store on SQLite 3.30 and later.
- Evaluation rounds eleven and twelve.

### 2026-10-26: version 0.1.0

- The first tagged version, a GitHub release with a wheel and a source archive. See the
  [CHANGELOG](../CHANGELOG.md#010-2026-10-26).
- Later changes are listed under [Unreleased](../CHANGELOG.md#unreleased).

## Live agent evaluations

In each round, independent coding agents got the docs, the `peeky` command and a new bug. They
couldn't read Peeky's source. Each report lists what the agents found and what was fixed.

| Round | Report |
| --- | --- |
| First | [2026-10-15: the CLI on the bug corpus](evaluations/2026-10-15-agent-cli.md) |
| A real program | [2026-10-15: CPython's `ipaddress`](evaluations/2026-10-15-real-program.md) |
| Docs alone | [2026-10-16: three languages from the docs alone](evaluations/2026-10-16-agent-cookbook.md) |
| Second | [2026-10-16: Java, C++ and Rust with stepping](evaluations/2026-10-16-agent-second-round.md) |
| Third | [2026-10-18: Rust floats, C integer time and Java objects](evaluations/2026-10-18-agent-third-round.md) |
| Fourth | [2026-10-19: Java aliasing, C overflow and a plain Rust crate](evaluations/2026-10-19-agent-fourth-round.md) |
| Fifth | [2026-10-21: Rust structs, a C unsigned sum and a Python default](evaluations/2026-10-21-agent-fifth-round.md) |
| Sixth | [2026-10-20: a Rust rate limiter, a C++ stock map and a C histogram](evaluations/2026-10-20-agent-sixth-round.md) |
| Seventh | [2026-10-20: a C ring buffer, a C++ scheduler and a Rust average](evaluations/2026-10-20-agent-seventh-round.md) |
| Eighth | [2026-10-21: a Java calculator, a Python grouper, a C++ router and a Rust leaderboard](evaluations/2026-10-21-agent-eighth-round.md) |
| Ninth | [2026-10-21: a C wrapper, Python sessions, Java invoices and a Rust table](evaluations/2026-10-21-agent-ninth-round.md) |
| Tenth | [2026-10-22: each language's cookbook as a fifth doc](evaluations/2026-10-22-agent-tenth-round.md) |
| Eleventh | [2026-10-23: C++ grades, a Rust rota, Java seats and C tides](evaluations/2026-10-23-agent-eleventh-round.md) |
| Twelfth | [2026-10-25: Python rainfall, Rust laps, a C++ playlist and Java desks](evaluations/2026-10-25-agent-twelfth-round.md) |

## Reviews and decisions

- [reviews/](reviews/) holds the independent reviews, with each finding's disposition.
- [adr/](adr/) holds the architecture decision records.
- [archive/](archive/README.md) holds early plans, kept as they were.
