# Where `peeky-first` spent its set-up

Round 2's [report](REPORT.md#what-to-do-next) found that asking agents to use Peeky first cost
8.6 more AI credits per bug than control, and that most of it went to set-up. This page
measures that set-up, from the 87 `peeky-first` attempts' transcripts, with no new runs. It is
the evidence for [ADR 0009](../../docs/adr/0009-first-question.md).

## How it was measured

[harness/setup_cost.py](harness/setup_cost.py) reads each attempt's session events and sorts
every tool call up to the first one whose output shows a Peeky stop. A call's category is the
first that applies: the stop; a Peeky run refused or ended with no stop; another Peeky run
(`describe`, `build`); starting a JVM to attach to; writing a Peeky config; reading Peeky's
docs, `--help` or source; the project's build or tests; waiting on a background command; and
anything else, mostly reading the project. The per-call records are in
`results/setup_cost.json`, with host paths removed, and the script makes the tables below from
them (`harness/setup_cost.py`; `--runs <raw runs>` reads the transcripts again).

Credits come from each model call's usage record. Two measures are used:

- **Credits to the first stop:** the session's credits up to the model call that asked for the
  stopping command. This counts everything, reading the project too, as control also does.
- **A call's credits:** its share of the model call that asked for it, plus what its output
  cost later. Each model call writes the previous calls' outputs to the prompt cache, and every
  later one reads them again: a 20 KB guide read early is paid for on every turn after it. The
  system prompt, the task and the model's own text aren't attributed to any call (a median of
  0.24 credits per model call).

Limits: the categories come from the command text, so a call that both reads the project and
cats a doc counts as a doc read, and only the docs' share of its output counts as docs.
Commands run inside a Python script are seen only when the script names `peeky`. Doc sizes are
the docs at `fef02c78`, the study's commit; an output too long for the tool reached the model
as a preview, so it counts as no doc bytes.

## What it shows

**Before its first stop, a median attempt made 13 tool calls and spent 11.1 credits and 71 s:
40% of its credits.** The set-up in it, by the second measure, was a median of 5.0 credits per
bug, against the 8.6 credits `peeky-first` cost over control. Docs were 4.2 of the 5.0.

| Language | Calls to the first stop | Credits | Seconds | Set-up credits per bug | Extra over control per bug |
| --- | --- | --- | --- | --- | --- |
| Java | 24 | 17.5 | 130 | 9.2 | +20.7 |
| C | 11 | 11.1 | 85 | 5.1 | +12.3 |
| C++ | never stopped | | | 5.6 | +8.6 |
| Rust | 10 | 8.7 | 64 | 4.5 | +8.4 |
| Python | 12 | 10.4 | 61 | 4.0 | +4.9 |

- **Docs, in every attempt.** All 87 read the 20 KB guide, `PROJECT.md` and their language's
  page (16 to 20 KB) before the first probe. Most Java, Rust and C/C++ attempts also read a
  cookbook section, and half ran `peeky probe --help`. A median of 5 calls and 33 KB of docs
  reached the model before the first stop. Several first `cat` the guide, got a preview, and
  read it again in parts.
- **Java: a JVM and an attach config, by hand.** No attempt used the guide's Maven or Gradle
  recipe (`-Dmaven.surefire.debug`, `--debug-jvm`). Instead they ran a reproducer: 16 of 21
  started `jshell` and 6 `java` with JDWP's agent, on a classpath copied out of surefire's
  reports or Lombok's build, and 4 started `javac` with Lombok's processor. That is 58 JVMs
  started by hand, about 3 per attempt, and 52 attach configs written, 50 of them inline in the
  probe command. Then 30 probes in 16 attempts ended with `NO_STOP`: the JVM they attached to
  never ran the line (the wrong classpath, the wrong JVM, or code that ran before the attach).
  The median Java attempt reached its first stop at call 24.
- **Refusals that a better message or a looser parser would have saved:**
  - Java: `Attach paths must be relative to the source root`, 6 times in 6 attempts, for an
    absolute path inside the root.
  - Python: `--set-at 'LINE NAME=VALUE'` as one word, an argparse usage error, 9 times in 8
    attempts. And `--run-module` with a module other than pytest (`timeit`,
    `sphinx.cmd.build`), 7 times.
  - Rust: `BUILD_REQUIRED` after git's index changed, 3 times, and `BUDGET_EXHAUSTED` on axum
    (#483), 5. Rust attempts mostly used the image's prebuilt config; 3 wrote their own.
  - C++: `UNSUPPORTED` for units without MD5s or with `-O0` after `-stdlib=libc++`, 17 times,
    on reproducers the agents compiled themselves, and `PROVENANCE_CHANGED`, 5.
- **What the first stop looked like:** Java's was an attach probe with a config (20 of 21),
  Python's `--run-module pytest` (17) or `--run` (7), C's `--binary` (6, the rest in scripts),
  and Rust's the image's config (7) or a session.

So the set-up has three parts, in this order of cost: docs read before the first probe,
Java's hand-started JVM and attach config, and refused runs. A command that takes the
project's own test command would remove the second and most of the third. A first-probe page
short enough to read in one call, which says the language page isn't needed yet, would cut the
first.

## Tables

The script's output, unedited.

### Up to the first stop (`peeky-first`, medians per attempt)

| Language | Attempts | Stopped | Calls | Credits | Seconds | Setup calls | Doc reads | Doc KB | Config writes | JVMs started by hand | Refused or failed Peeky runs |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| c | 12 | 12 | 11 | 11.1 | 85 | 6 | 4 | 35 | 0 | 0 | 0 |
| cpp | 9 | 0 | nan | nan | nan | 10 | 7 | 32 | 0 | 0 | 3 |
| java | 21 | 21 | 24 | 17.5 | 130 | 16 | 6 | 36 | 3 | 3 | 2 |
| python | 27 | 27 | 12 | 10.4 | 61 | 6 | 4 | 29 | 0 | 0 | 1 |
| rust | 18 | 13 | 10 | 8.7 | 64 | 6 | 6 | 39 | 0 | 0 | 1 |
| all | 87 | 73 | 13 | 11.1 | 71 | 7 | 5 | 33 | 0 | 0 | 1 |

Calls, credits (the session's, cumulative) and seconds run to the call that showed the first stop, in attempts that stopped. The other columns count the calls before the first stop, or all calls if there was none. Setup calls are docs, config, debuggee, wait, and Peeky runs that didn't stop. A doc read is any call that read Peeky's docs, `--help` or source; Doc KB is the docs' share of what those calls printed. A config write is any call that wrote one, inline ones included.

### Where the extra credits went (medians per bug)

| Language | Bugs | control | peeky-first | Difference | Setup before the first stop | of which docs | Peeky after the first stop |
| --- | --- | --- | --- | --- | --- | --- | --- |
| c | 4 | 19.1 | 31.3 | +12.3 | 5.1 | 4.7 | 0.1 |
| cpp | 3 | 18.0 | 23.6 | +8.6 | 5.6 | 4.1 | 0.0 |
| java | 7 | 26.4 | 51.2 | +20.7 | 9.2 | 5.6 | 0.0 |
| python | 9 | 18.1 | 24.6 | +4.9 | 4.0 | 3.2 | 0.5 |
| rust | 6 | 17.4 | 27.1 | +8.4 | 4.5 | 4.2 | 0.9 |
| all | 29 | 18.1 | 29.5 | +8.6 | 5.0 | 4.2 | 0.3 |

A call's credits: its share of the output of the model call that asked for it, plus what its output cost in context afterwards (written to the cache once, read again by each later model call). The system prompt, the task and the model's own text aren't attributed to a call. Set-up is the set-up calls' credits, and the docs' share of a call that read docs among other things.

### Calls before the first stop, by category (all attempts)

| Language | docs | config | debuggee | peeky_refused | peeky_other | build_test | wait | code | stop |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| c | 65 (73 cr) | 0 (0 cr) | 0 (0 cr) | 4 (1 cr) | 11 (10 cr) | 0 (0 cr) | 0 (0 cr) | 60 (60 cr) | 12 (11 cr) |
| cpp | 55 (60 cr) | 0 (0 cr) | 0 (0 cr) | 25 (10 cr) | 6 (5 cr) | 43 (52 cr) | 0 (0 cr) | 105 (89 cr) | 0 (0 cr) |
| java | 134 (176 cr) | 1 (1 cr) | 60 (31 cr) | 38 (8 cr) | 9 (11 cr) | 6 (5 cr) | 77 (17 cr) | 140 (159 cr) | 21 (9 cr) |
| python | 120 (137 cr) | 0 (0 cr) | 0 (0 cr) | 26 (13 cr) | 12 (15 cr) | 2 (2 cr) | 0 (0 cr) | 133 (118 cr) | 27 (13 cr) |
| rust | 89 (91 cr) | 0 (0 cr) | 0 (0 cr) | 19 (10 cr) | 7 (2 cr) | 43 (44 cr) | 0 (0 cr) | 102 (88 cr) | 13 (11 cr) |
| all | 463 (536 cr) | 1 (1 cr) | 60 (31 cr) | 112 (43 cr) | 45 (43 cr) | 94 (103 cr) | 77 (17 cr) | 540 (514 cr) | 73 (44 cr) |

Each cell: calls, and their credits as above, summed over attempts.

### Refused or failed Peeky runs before the first stop

| Language | Code | Runs | Attempts |
| --- | --- | --- | --- |
| c | UNSUPPORTED | 2 | 2 |
| c | INVALID_BREAKPOINT | 1 | 1 |
| c | INVALID_REQUEST | 1 | 1 |
| cpp | UNSUPPORTED | 17 | 6 |
| cpp | PROVENANCE_CHANGED | 5 | 5 |
| cpp | INVALID_BREAKPOINT | 3 | 3 |
| java | NO_STOP | 30 | 16 |
| java | INVALID_REQUEST | 7 | 7 |
| java | DEBUGGER_ERROR | 1 | 1 |
| python | INVALID_REQUEST | 9 | 8 |
| python | USAGE | 9 | 8 |
| python | NO_STOP | 7 | 6 |
| python | NOT_FOUND | 1 | 1 |
| rust | BUDGET_EXHAUSTED | 5 | 3 |
| rust | INVALID_REQUEST | 5 | 4 |
| rust | INVALID_BREAKPOINT | 4 | 3 |
| rust | BUILD_REQUIRED | 3 | 3 |
| rust | DEBUGGER_ERROR | 1 | 1 |
| rust | PROVENANCE_CHANGED | 1 | 1 |

### Peeky docs read before the first stop

| Language | File | Attempts that read it |
| --- | --- | --- |
| c | AGENT_GUIDE.md | 12 of 12 |
| c | PROJECT.md | 12 of 12 |
| c | lang/c-cpp.md | 12 of 12 |
| c | lang/c-cpp-cookbook.md | 7 of 12 |
| c | --help | 6 of 12 |
| cpp | AGENT_GUIDE.md | 9 of 9 |
| cpp | PROJECT.md | 9 of 9 |
| cpp | lang/c-cpp.md | 9 of 9 |
| cpp | lang/c-cpp-cookbook.md | 8 of 9 |
| cpp | --help | 2 of 9 |
| java | AGENT_GUIDE.md | 21 of 21 |
| java | PROJECT.md | 21 of 21 |
| java | lang/java.md | 21 of 21 |
| java | lang/java-cookbook.md | 19 of 21 |
| java | --help | 16 of 21 |
| java | JAVA_ATTACH.md | 7 of 21 |
| python | AGENT_GUIDE.md | 27 of 27 |
| python | PROJECT.md | 27 of 27 |
| python | lang/python.md | 27 of 27 |
| python | --help | 16 of 27 |
| python | lang/python-cookbook.md | 1 of 27 |
| rust | AGENT_GUIDE.md | 18 of 18 |
| rust | PROJECT.md | 18 of 18 |
| rust | lang/rust.md | 18 of 18 |
| rust | lang/rust-cookbook.md | 14 of 18 |
| rust | --help | 10 of 18 |

### Config writes and debuggee starts before the first stop

| Language | Java attach | Rust workspace | Native | Of these, inline | JVM started by hand | Classpath hunts |
| --- | --- | --- | --- | --- | --- | --- |
| c | 0 | 0 | 0 | 0 | 0 | 0 |
| cpp | 0 | 0 | 0 | 0 | 0 | 0 |
| java | 52 | 0 | 0 | 50 | 58 | 2 |
| python | 0 | 0 | 0 | 0 | 0 | 0 |
| rust | 0 | 3 | 0 | 3 | 0 | 0 |
| all | 52 | 3 | 0 | 53 | 58 | 2 |

### How the Java attempts started the JVM they attached to

| Started with | Attempts | Starts |
| --- | --- | --- |
| jshell | 16 | 44 |
| java | 6 | 8 |
| javac | 4 | 4 |
| other | 1 | 2 |
