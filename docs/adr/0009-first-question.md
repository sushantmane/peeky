# ADR 0009: The first question, from the project's own test command

Status: accepted, from the owner's "lets do" on round 2's next steps. The evidence is
`experiments/value-study-2/SETUP_COST.md`.

## Context

Round 2 of the value study asked agents to use Peeky before editing. They did, in all 87
attempts, and it cost a median of 8.6 more AI credits, 88 s and 7 tool calls per bug than
control. Credits and wall time were higher on all 29 bugs; tool calls on 28
(`experiments/value-study-2/REPORT.md`). The transcripts say where that went (`SETUP_COST.md`
beside it):

- **Before the first stop,** in the 68 of 87 attempts that stopped, the median attempt made 14
  tool calls, spent 11.6 credits and 75 s: 41% of its credits. By call, set-up was 5.3 credits
  per bug, and reading Peeky's docs 4.2 of it.
- **Docs:** 86 of 87 attempts read the 20 KB guide, `PROJECT.md` and their language's page
  before their first probe; the other read its language's page one call after. 48 of the 60
  Java, Rust and C/C++ attempts read a cookbook section too.
- **Java** cost most: a median of 24 calls and 17.5 credits to the first stop. No attempt used
  the guide's Maven or Gradle recipe. They started JVMs by hand (`jshell`, `java`, `javac` with
  JDWP's agent) on classpaths copied from surefire's reports or Lombok's build: the median Java
  attempt started 3, and all 21 started at least one. 51 of their 53 attach configs were
  written inline in the probe command. 30 probes in 16 attempts then ended `NO_STOP`: the line
  didn't run while Peeky was attached.
- **Refusals** a parser or a message could have saved: an absolute path in an attach config
  (6), `--set-at 'LINE NAME=VALUE'` as one word (9), `--run-module` with a module other than
  pytest (7), and Rust's `BUILD_REQUIRED` after git's index changed (3).

Each language already has a first command, but each needs something only the agent can supply:
Python the right interpreter, `--run` or `--run-module`; Java a JVM waiting for a debugger and a
config naming its sources and classes; Rust a config naming what to analyse and run, then
`peeky build`; C and C++ the binary the test runs. The agent already has the one thing that
names all of them: the command that runs the failing test.

## Goal

An agent with only the failing test's command gets its first value in 2 or 3 Peeky calls, with
no config written by hand, after reading one page of about 2 KB. Stateless probes stay the
default, and sessions stay available. The core rule doesn't bend: Peeky never shows a wrong or
guessed value, or makes a misleading claim, without a note. A launcher that can't prove what it
attached to refuses.

## Decision

### 1. `--test`: the project's own test command

```sh
peeky probe . --at FILE:LINE --capture a,b.c --test -- <the command that runs the failing test>
```

The words after `--` are the test command, as the agent would type it, from the directory it
runs in. Peeky reads it, picks the target it names, and runs it under the debugger. `--test`
takes the place of `--config`, `--run`, `--run-module`, `--python`, `--binary` and `--arg`; with
any of them it is refused, naming the one to drop. `session start` takes it too.
Environment assignments before the command are set for a Python or Java run, as they would be in
front of `peeky`. Peeky runs Cargo and a native binary in an environment of its own, so there an
assignment that wouldn't reach them is refused, but for `PATH`, `HOME`, `TMPDIR` and the locale.
A `RUSTFLAGS` of lints alone (`-Awarnings`, as the study's hints give) changes no code: it gets a
note.

| Command | Peeky runs | Refused, and the next step |
| --- | --- | --- |
| `pytest …`, `python -m pytest …` | `--run-module pytest -- …`, on the interpreter that runs that `pytest` or `python` (`PATH`, or the script's `#!` line) | `python -m <other module>`: `--run-module` runs pytest only (#447); the message names `--run <script>` |
| `python script.py …`, `./script.py …` | `--run script.py -- …`, on that interpreter | A script outside the root |
| `cargo test [-p P] [--test T \| --lib \| --bin B] [--features …] [filter] [-- args]` | A Rust workspace config, below | `--release`, `--doc`, `--workspace`, `--example`, `--no-default-features`, `--all-targets`: each named, with why |
| `./prog args` (C, C++) | `--binary ./prog -- args` | A binary outside the root; `make test`, `ctest` without one test, scripts: the message says to name the test's binary |
| `ctest [--test-dir D] -R NAME` | The one test's command, from `ctest --show-only=json-v1` | More or fewer than one test; a test whose working directory isn't the root |
| `mvn`, `./mvnw`, `gradle`, `./gradlew`, `java`, `javac` | Section 2 | Ant, sbt, Bazel and other tools: section 2 |

Python, C and C++ change only how a target is named: what runs, and every check after it, is
today's. `--worker` with `--test` is refused: a worker runs only what its own configuration
names. So the worker protocol doesn't change.

### 2. Java: Peeky starts the test, finds its JVM, and proves it attached to it

Peeky runs the command with JDWP's agent added to the JVM that runs the tests, and never to
the build tool's own:

| Tool | What Peeky adds |
| --- | --- |
| Maven | `-Dmaven.surefire.debug=<agent> -Dpeeky.launch=<nonce>`, and the same for failsafe. Surefire adds them to the JVMs it forks. A command with `forkCount=0` (tests in Maven's own JVM) or its own `maven.surefire.debug` is refused before it runs |
| Gradle | `-I <init script>`: after the build is configured, every `Test` task gets the agent and the nonce in its `jvmArgs`, and `maxParallelForks = 1`. A command with `--debug-jvm` is refused |
| `java` | The agent and the nonce as its first options |
| `javac` | The same with `-J`, for annotation processors such as Lombok's |
| Ant and the rest | Refused before anything runs. Ant has no switch for the JVMs its `junit` task forks, and `JAVA_TOOL_OPTIONS` would reach Ant's own JVM first. The message says to give the `java` command the test runs |

The agent is `-agentlib:jdwp=transport=dt_socket,server=y,suspend=y,address=127.0.0.1:0`: each
test JVM waits for a debugger before any code runs, on a free loopback port the JVM picks. So
port 5005, and any JVM left over from an earlier run, play no part. The nonce is 128 random
bits, new for each run.

**Which JVM.** Peeky looks for a process of its own user whose JVM options, before the main
class, hold both words exactly: the agent and `-Dpeeky.launch=<nonce>`. The build tool's JVM
doesn't qualify, though Maven's holds the nonce inside its `-Dmaven.surefire.debug=…` argument;
nor does a shell that passes the words on. Its port is the one loopback port it listens on,
read from the operating system (`/proc` on Linux, `lsof` on macOS), not from the build's
output. A JVM listening on none, or more than one, is refused. Linux and macOS only.

**The proof.** After the debugger attaches, and before the JVM runs, Peeky checks that the JVM
holds an established connection on its port whose other end is the debugger Peeky started.
Without it, nothing is read: the probe fails, and says why.

**Where the classes are.** Peeky declares the `--at` file, and the class roots that hold its
classes: Maven's `target/classes` and `target/test-classes`, Gradle's `build/classes/<lang>/…`,
under the file's module, and for `java` and `javac`, the directories in the command's
classpath. The attach checks are today's: the classes' line tables must fit the source, and
sources and classes are hashed and checked again before every read. As with any attached JVM,
Peeky can't prove the JVM loaded those bytes; the result keeps saying `attached`.

**The build is Peeky's.** It runs in its own process group, its output in Peeky's state
directory. When the probe has its hits, it detaches and lets the test run on, then waits for
the build, within `--timeout`, and reports its exit code and last lines. At the deadline it
stops the build. A second test JVM that starts and waits is stopped too, and a `NO_STOP` says
that the line may run in it. If the build ends without a JVM ever waiting, that is `NO_STOP`,
with the build's last lines and the likely cause.

**Time.** With `--test`, `--timeout` defaults to 300 s: it covers the build. `--background`
works as before.

Java sessions with `--test` are refused for now: the build would have to outlive the command
that started it, and belong to the session's worker. Attaching a session as today still works.

### 3. Rust: the config from `cargo test`'s words

Peeky makes the workspace config ([ADR 0008](0008-native-project-reach.md), phase 2) from the
command and `cargo metadata`:

- **analyse:** the package whose directory holds the `--at` file.
- **run:** `--test T`, `--bin B` or `--lib`, in `-p`'s package. Without `-p`, in the packages
  Cargo selects: the root's package, or `default-members`, or else every member; the one with
  that target. Without a target, the `--at` file decides: `tests/T.rs` runs `T`. A library file
  with no target named is refused, listing the package's test targets to choose from. So is an
  `--at` file outside the packages Cargo selects: add `-p`.
- **features:** `--features` and `-F`. `--all-features` lists the package's own, and those its
  optional dependencies give, as Cargo turns them on.
- **arguments:** the test name filter and the words after cargo's `--`.
- **vendor:** `vendor` when it exists. Otherwise the refusal names `cargo vendor --locked vendor`.

Peeky saves the config in its state directory and names it. It builds when no build of that
config is current, as `--build` does, and says so; otherwise it probes. A build refused because
git refreshed `.git/index` while it ran, its entries the same (#486), is built once more: that
refusal's own next step, which `--test` takes for the agent. A command without `-p`
in a workspace of several packages gets a note: Peeky builds one package, so Cargo may unify
fewer features than the command would.

### 4. One short page, first

`docs/FIRST.md`, at most about 2 KB: one copy-paste command per language, three rules (a
breakpoint stops before its line runs; read every warning; a note isn't a value), and where to
go when refused. It says the language page isn't needed for the first probe. The guide's first
lines point to it, and `peeky probe --help` shows its command first.

### 5. Refusals that name the next command

| Refusal in round 2 | Change |
| --- | --- |
| `Attach paths must be relative to the source root` (6) | A path inside the root is taken, absolute or not; outside, the message names the root |
| `--set-at 'LINE NAME=VALUE'`, one word (9) | Taken when the word is a location, a space, then `NAME=VALUE`; a path with a space still needs two words |
| `--run-module` with another module (7) | The message gives the `--run` form for it |
| `NO_STOP` on an attached JVM (30) | The hint says the JVM may not run the line, and gives the `--test` form |
| `BUILD_REQUIRED` (3) | With `--test`, Peeky builds |

## What is refused, and why

| Refused | Why |
| --- | --- |
| A Java JVM Peeky can't tie to its own run | Attaching to another JVM would show its values as the test's |
| More than one listening port on the test JVM | Peeky can't tell which is the debugger's |
| Ant, and tools Peeky can't add the agent to | The agent would reach the build tool's JVM, or none |
| `forkCount=0`, the command's own `--debug-jvm` or `maven.surefire.debug` | The tests would run in the build's JVM, or wait on a port Peeky didn't choose |
| `cargo test --release`, `--doc`, `--workspace`, `--example` | Peeky builds one test target at opt-level 0 |
| `--test` with `--worker` | A worker runs only what its own configuration names |
| A shell line (`bash -c …`, `&&`) | Peeky runs one command, not a shell |

## Consequences

- An agent needs one page and one command per question. In round 2's images, that removes the
  hand-made JVMs and configs, the refusals they caused, and most of the docs.
- Peeky now runs a project's build, as the agent would. It is the project's code: trusted, as
  the program is, and not sandboxed.
- The Java launcher reads other processes' command lines and sockets, of the same user only.
- The study's measure is set: on round 2's images, with no agent, the `--test` command reaches
  the fix's line in fewer calls than the transcripts took. A later round can measure cost.
