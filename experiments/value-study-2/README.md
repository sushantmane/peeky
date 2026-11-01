# Does Peeky change the outcome? Round 2

Round 1 ([../value-study](../value-study/README.md), issue #441) found no difference. Both arms
solved about 9 bugs in 10, and agents ran Peeky in 3 of 87 attempts. Peeky printed no program
value, and it couldn't reach 13 of the 29 projects. Its
[report](../value-study/REPORT.md#what-to-do-next) asked for the reach work, a clearer guide, and
a second study. Since then, these merged to `main`:

- #449: Java runs the adapter on its bundle's JDK, so a JDK 11 project works (round 1's refusal).
- #445: the guide says when a probe beats a print, and gives a first command per project type.
- #451: `verify --check-command` checks an edit with the project's own tests.
- #453 and #454: breakpoints in installed packages, and `--run-module pytest`.
- ADR 0008 phase 1 (#452, #456, #458, #460, #464): debug the binary a project built with clang,
  with `--binary`.
- ADR 0008 phase 2, parts 1 to 3 and 5 (#450, #455, #459, #468): Rust workspaces with
  dependencies, `peeky build`, and edition 2018.

This round runs the same 29 bugs again, in three arms, and first checks, with no agent, where
Peeky can stop. The protocol below is fixed before any agent run. Changes after that go under
"Deviations", with the reason.

## The design

- **Part A:** the 29 bugs, three arms, 3 attempts each: 261 attempts.
  - `control`: round 1's control arm, run again. The CLI or the model may have drifted.
  - `peeky`: Peeky from the current `main`, and round 1's prompt paragraph, unchanged.
  - `peeky-first`: the prompt asks the agent to reproduce the failure and read the failing state
    with Peeky before it edits. It may decide Peeky can't reach the code, and say why.
- **The reach check:** before any attempt, a script probes each bug where its fix changes the
  code, and at its failing test's first assertion, in the Peeky image. No agent and no model.
  It records whether Peeky stopped there, refused (and why), or wasn't tried. This measures what
  the reach work bought, apart from what agents do with it.

## Bugs

The same 29 bugs as round 1, listed in [../value-study/BUGS.md](../value-study/BUGS.md). Their
problem statements, hidden tests, upstream fixes, grading and control images are round 1's,
unchanged. No bug is added or removed.

## Arms

Everything not listed here is as in round 1's [protocol](../value-study/README.md#arms).

- **Agent:** GitHub Copilot CLI with the shell as its only tool. Round 1 ran 1.0.105. The
  organization's policy sets the model, `gpt-6.1-sol` in round 1. Each attempt records both.
- **Control image:** `peeky-study/<id>:control`, round 1's image. It wasn't rebuilt.
- **Peeky image:** `peeky-study/<id>:peeky-r2`, which both Peeky arms use. It is described below.
- **Prompts:** every arm gets round 1's [prompt.md](../value-study/harness/prompt.md).
  - `peeky` adds round 1's [prompt-peeky.md](../value-study/harness/prompt-peeky.md), word for
    word: "Peeky, a debugger built for agents, is installed (`peeky`). Start with
    `/opt/peeky/docs/AGENT_GUIDE.md`. Use it if it helps."
  - `peeky-first` adds [prompts/prompt-peeky-first.md](prompts/prompt-peeky-first.md) instead:
    > Peeky, a debugger built for agents, is installed (`peeky`). Start with
    > `/opt/peeky/docs/AGENT_GUIDE.md`. Before you edit any file, reproduce the failure, then use
    > Peeky to stop where the program goes wrong and read its values there. If Peeky can't reach
    > that code, say why, quoting its message, and go on without it. In your final paragraph,
    > also say what Peeky showed you, or why you couldn't use it.
- **Budgets:** 40 minutes and 250 AI credits per attempt, as in round 1.
- **Repeats and order:** 3 attempts per bug per arm. The 261 attempts run in an order shuffled
  with seed 20261101, at most 3 at once.
- **Pilot:** as in round 1, one bug once in each arm, to check the harness. It doesn't count.

### The Peeky image

[docker/peeky.Dockerfile](docker/peeky.Dockerfile) builds it from the bug's control image, and
[docker/prepare.sh](docker/prepare.sh) adds what each project needs. Peeky is a wheel built from
one `main` commit, recorded in the image's label and in every attempt's result.

- **Peeky and its docs:** as in round 1, in `/opt/peeky`. The docs are the guide, the cookbook,
  the error codes and the language pages.
- **This container's page:** `/opt/peeky/docs/PROJECT.md` says what the image prepared for Peeky.
  One line under the guide's title points to it. Without it, the agent couldn't know where
  Peeky's build of a C or C++ project is.
- **Java:** `peeky setup-java` runs with JDK 17 first on `PATH`, as the guide asks. The adapter
  then runs on JDK 17 and debugs the JDK 11, 17 and 21 projects alike.
- **C and C++:** clang 18, lldb-dap 18 and llvm-dwarfdump 18 are in the base image, in both arms.
  The Peeky image also builds each project for Peeky, as the next section says.
- **Rust:** the toolchain is the base image's Rust 1.96, which Peeky's Cargo checks expect.
  `cargo vendor --locked vendor` writes the dependencies to `/testbed/vendor`, so `peeky build`
  works with no network. Git ignores the directory, and the project's own Cargo builds don't
  read it.
- **Python:** nothing more. The project's virtual environment is active, and Peeky runs on it.

### C and C++: the image builds the project for Peeky

Decision: yes. Peeky reads a binary that clang built at `-O0`, with DWARF 5 and its command line
recorded ([C and C++ 7.1](../../docs/lang/c-cpp-cookbook.md#debug-a-binary-your-project-built)).
The control images build with each project's own flags, GCC and `-O2` or `-O3`, which Peeky
refuses. A pre-built binary leaves the agent one step, `--binary`, as the owner asked. This is a
difference from the control arm, listed below.

The flags are the cookbook's: `-O0 -g -gdwarf-5 -grecord-command-line -fdebug-macro`, and a build
ID. They are the same as `tests/projects/build.sh` on #473's branch, which the phase 1 acceptance
uses. prepare.sh doesn't call that script, for three reasons:

- valkey's tests need `valkey-cli` too, and build.sh builds only `valkey-server`.
- jq's rebuild must keep `bison -l`, so prepare.sh records `YFLAGS=-l` in configure.
- MicroPython's own build must stay as the control image has it, so Peeky's goes in its own
  directory.

| Bug | Peeky's build | The project's own build | Rebuild after an edit |
| --- | --- | --- | --- |
| jq-2650 | `./jq`, in place | replaced: `make` and `make check` use Peeky's | `make -j4` (configure kept the flags) |
| valkey-1842, valkey-790 | `src/`, in place | replaced: `./runtest` uses Peeky's | the `make` line in PROJECT.md; a plain `make` brings back the project's own |
| micropython-13569 | `ports/unix/build-peeky/micropython` | unchanged (`build-standard`) | the `make` line in PROJECT.md |
| fmt-3158, fmt-3272 | `build-peeky/`, with libc++ | unchanged (`build/`) | `cmake --build build-peeky --target <test>` |
| json-4237 | `build-peeky/`, with libc++ | unchanged (`build/`) | `cmake --build build-peeky --target <test>` |

- C++ builds with libc++, as the cookbook asks: libstdc++'s headers leave a unit with no MD5s. The
  Peeky image installs `libc++-18-dev` for fmt and json.
- jq and valkey build only in their source trees, so there Peeky's build replaces the project's
  own. Their tests then run on a clang `-O0` build in the Peeky arms.
- Grading is unaffected. Every C and C++ `eval.sh` configures or cleans, then rebuilds the
  project's own build from scratch, as in the control arm.

### Grading in the Peeky image

Grading was checked in all 29 Peeky images, with no agent, as round 1 checked its images. The
upstream fix is solved, and an unchanged checkout isn't (`harness/study.sh run_one.py <bug> peeky 1
--fake-agent gold|noop`). One needed care: jq's upstream fix also edits the generated
`src/parser.c`, which doesn't apply to the image's regenerated one. With the fix to `src/parser.y`
alone, grading regenerates the parser, and the attempt is solved.

### What differs from the control image

- Peeky, its docs, PROJECT.md and the guide's pointer line.
- The Java adapter bundle, built on JDK 17.
- jq and valkey: the working tree starts built with clang at `-O0`. jq's generated `src/parser.c`,
  `parser.h`, `lexer.c` and `lexer.h` differ from the commit (bison `-l` and flex `-L`, no
  `#line`). They show in `git diff` and in the attempt's patch, but grading regenerates them.
- MicroPython, fmt and json: a `build-peeky` directory, which git ignores.
- fmt and json: libc++ installed.
- Rust: `/testbed/vendor`, which git ignores.

## Outcomes

- **Primary:** the paired difference in solve rate, `peeky-first` − `control`, and `peeky` −
  `control`. Each is the mean over bugs of the bug's rate in one arm minus its rate in the other.
  Its 95% interval resamples bugs, as in round 1 (10,000 resamples). The two are reported side by
  side, with no correction for multiplicity.
- **Secondary:**
  - Peeky commands per attempt, and the attempts that ran Peeky at all, per Peeky arm.
  - Program values Peeky printed per attempt: rows of a probe's table, a session's
    `name: type = value` lines, and `"value"` fields of its JSON. The count is approximate; the
    second judge (below) checks the values.
  - AI credits, wall time and tool calls per bug: the paired median difference, as in round 1.
  - Wrong root causes: the blind judge of round 1 ([judge_prompt.md](../value-study/harness/judge_prompt.md)),
    on every attempt. A second, unblinded judge checks the values Peeky printed against the
    program, in attempts that ran Peeky.
  - By category and by language, as exploration only.
- **By reach group** ([below](#reach-groups-and-the-prs-they-wait-on)): the primary outcomes
  within the bugs Peeky reaches and within the others, as exploration.
- **Descriptive** ([harness/descriptive.py](harness/descriptive.py)): this round's control rate
  against round 1's, bug by bug, to show drift. And Peeky's uptake in each reach group: the share
  of attempts that ran Peeky and that printed a program value.

## Analysis

`harness/study.sh analyze.py` writes `results/summary.json` and the report's tables. It is round
1's analysis, run with this round's [study.json](study.json): the arms, the comparisons and the
seed. Round 1's output is unchanged by the code that adds a third arm: run on round 1's results,
it gives the same report and the same `summary.json`, byte for byte.

## Exclusions

- An infrastructure failure is retried at most twice, as in round 1. If it still fails, the
  attempt is excluded and listed.
- A control attempt that ran Peeky is contaminated and excluded.
- A `peeky-first` attempt that didn't run Peeky counts as it is. The comparison is of prompts,
  not of attempts that complied.
- No bug is left out after the runs. A bug whose images fail their check before the runs is left
  out of every arm, and the reason is listed.

## Seeds

- The attempts' order, the bootstrap and the judges' order: 20261101.
- The reach check has no randomness.

## The reach check

[harness/reach.py](harness/reach.py), with the points in
[harness/reach_specs.json](harness/reach_specs.json). The points were fixed before the first
reach run, by these rules:

- **Gold points:** in the project's source files that the upstream fix changes, the first and the
  last hunk that changes an executable statement. Tests, docs, changelogs, imports, declarations
  and signatures don't count. A hunk's point is the first line it removes or changes. For a pure
  insertion, it is the statement just after the insertion, in the same function.
- **A generated file:** jq's fix changes `src/parser.y` and the `src/parser.c` that bison
  generates from it. Both are probed: the grammar action, and the same action in the image's
  `parser.c`, found by its text.
- **The assertion point:** the first assertion that the first failing test runs, in its body or
  in a helper it calls first. Its line is in the file after the hidden test patch. When the test
  is data that a harness checks, the point is the harness's comparison, if the harness is in the
  project's language. Otherwise it isn't attempted, with the reason.
- **The run:** each point runs what the failing test runs: the test itself, or for a Rust binary
  or a server, the program with the test's arguments. A server is driven to the line by commands,
  such as valkey's `ACL LOAD` on a replica.

Each point is tried as an agent would, with a session: `peeky session start`, `session inspect`
at the stop, then `session close`. Java attaches to the test's JVM, started waiting for a
debugger. Rust uses a workspace config and `--build`. Each container is fresh, with no network,
4 CPUs and 6 GB, one at a time. The hidden test patch is applied first, so the failing test
exists.

Each point's outcome is one of:

- **reached:** the program stopped there. `inspect` gives how many values it read, and how many
  it withheld with a note.
- **refused:** Peeky refused, with its error code and message. A refusal is tried once more
  when Peeky marks it retryable, or its message says to build or run again. It is also tried
  again after a remedy its message names, where the spec lists that remedy and it leaves the
  program as it is: a lockfile's `version = 3` line. Every answer is kept.
- **no stop:** the program ran and ended, or stopped elsewhere, without stopping there. The
  program's exit and output say why.
- **not attempted**, **timed out** or **error**, with the reason.

The check runs on the `main` of the day, and again on the `main` that Part A uses. Every run's
records are kept, one directory per Peeky commit. [REACH.md](REACH.md) has the results, and
which bugs depend on which open PR.

A first pass on `791510a6` found two problems in reach.py, fixed before the runs recorded here.
valkey-790's server had no directory for its cluster file, and a "Build again" refusal wasn't
tried again. The lockfile remedy was added then too. That pass's records aren't kept.

One point moved after the runs on `791510a6`. druid's assertion point was the first assertion in
its test's text (line 573), but the test first calls a helper that asserts. It is now that
helper's first assertion (line 1018), as the rule says. The run on `791510a6` keeps line 573.

## Reach groups, and the PRs they wait on

This section is fixed before Part A, like the rest. On 31 October, the Rust and C and C++ bugs'
reach depends on PRs that are still open. Part A starts once they merge. Then the reach check
runs on the `main` that Part A uses, before any attempt, and its records fix each bug's group:

- **Reached:** a gold point stopped, and `session inspect` read at least one value there.
- **Not reached:** every other bug. Two kinds are reported apart:
  - **refused:** Peeky refused every gold point;
  - **other:** Peeky stopped but read no value, or the failing test never ran a gold line.

The analysis reports the two primary differences within each group, and Peeky's uptake there
(`harness/descriptive.py`). These splits are exploration: the groups are small, and bugs aren't
assigned to them at random. The overall results stay the primary outcomes.

Python and Java wait on no open PR. On `42878bbd`, 15 of their 16 bugs were reached. astropy's
failing test fails before it runs any changed line, so it is "other".

| Bug | On `42878bbd` | Waits on | Likely on the final `main` |
| --- | --- | --- | --- |
| jq-2650 | reached (the fix's action in `parser.c`) | #469 (its harness's 3 withheld locals), #473 | reached |
| micropython-13569 | reached | #469, #473 | reached |
| valkey-1842 | stops; every local withheld (`__has_include`) | #477, #469, #473 | reached |
| valkey-790 | stops; every local withheld (`__has_include`) | #477, #469, #473 | reached |
| fmt-3158 | refused: no unit with the fix's code is judged | #480 and #477 (`test-main.cc` only), #473 | refused, until phase 3 |
| fmt-3272 | refused: no unit with the fix's code is judged | #480 and #477 (`test-main.cc` only), #473 | refused, until phase 3 |
| json-4237 | refused: the binary, whole | #473 | refused, until phase 3 |
| tokio-4867 | stops in generic code; `inspect` fails | #463 | refused: generic code that another crate compiles |
| ripgrep-2209 | stops in generic code; `inspect` fails; its test's build fails | #463, #481 | refused: generic code that another crate compiles |
| tokio-6752 | stops in generic code; `inspect` fails | #463 | refused: generic code that another crate compiles |
| axum-1934 | refused: `BUDGET_EXHAUSTED` | #483, #463 | depends on #483 |
| bat-3108 | refused: its build fails, intermittently | #481, #463 | depends on #481 |
| coreutils-6690 | refused: a build script reads `CARGO_BUILD_RUSTFLAGS`; its test's build fails | #482, #481, #463 | depends on #482 and #481 |

- Every Rust bug also waits on #463 for the guide: on `42878bbd`, it still says crates with
  dependencies aren't supported. That changes what an agent tries, not what the reach check finds.
- The last column is a forecast. The group is what the final reach run records.
- If a listed PR hasn't merged when Part A starts, the report says so. The groups still come from
  the final reach run.

## Deviations from round 1

All were decided before any round 2 run.

- **A third arm,** `peeky-first`, and a rerun of the control arm rather than reuse of round 1's
  control data.
- **A new Peeky image:** the C and C++ projects built for Peeky, the Rust dependencies vendored,
  `setup-java` on JDK 17, libc++, and PROJECT.md with the guide's pointer line.
- **Seed 20261101** instead of 441, so the order is new.
- **A new measure:** program values Peeky printed per attempt.
- **The reach check,** which round 1 didn't have.
- **The harness:** round 1's code runs this round through a study config. Its Peeky arm checks
  became "any arm with Peeky", and its analysis takes a list of comparisons. Round 1's results
  give the same output as before.

## Cost estimate

From round 1: 174 attempts took 3,716 AI credits, 21.4 per attempt (control 21.0, Peeky 21.7).
They took 6.5 machine-hours with grading, and 2.2 hours of wall time at 3 at once.

| Arm | Attempts | Credits | Machine-hours |
| --- | --- | --- | --- |
| control | 87 | about 1,830 | 3.3 |
| peeky | 87 | about 1,890 | 3.2 |
| peeky-first | 87 | 1,890 to 3,780 | 3.2 to 6.4 |
| **Part A** | **261** | **5,600 to 7,500** | **9.7 to 12.9** |

- `peeky-first` is assumed to cost 1 to 2 times the `peeky` arm: running Peeky costs commands and
  output to read. The middle of the range is about 6,600 credits.
- At round 1's ratio of wall time to machine time, Part A takes 3.3 to 4.4 hours at 3 at once.
- The per-attempt cap bounds the worst case at 65,250 credits. Round 1's costliest attempt used 79.
- Not counted: the judges, as in round 1, and the pilot (3 attempts).

## Reproducing

```sh
# Images: round 1's control images, then this round's Peeky images.
PEEKY_CHECKOUT=<peeky checkout at the commit to study> harness/build_images.sh
# The reach check, then its tables.
harness/reach.py
harness/reach.py --report results/reach/<sha, 12>
# Part A, with round 1's harness and this round's config.
harness/study.sh schedule.py --dry-run
harness/study.sh schedule.py --concurrency 3 --budget-minutes 40 --budget-credits 250
harness/study.sh prepare_judging.py results/runs <judge dir>   # then judge, as in round 1
harness/study.sh analyze.py
```

## Files

| File | What it is |
| --- | --- |
| [study.json](study.json) | The arms, their images and prompts, the comparisons and the seed. |
| [prompts/prompt-peeky-first.md](prompts/prompt-peeky-first.md) | The `peeky-first` arm's paragraph. |
| [docker/peeky.Dockerfile](docker/peeky.Dockerfile), [docker/prepare.sh](docker/prepare.sh) | The Peeky image. |
| [harness/build_images.sh](harness/build_images.sh) | Builds the Peeky images with round 1's script. |
| [harness/study.sh](harness/study.sh) | Runs a round 1 harness script with this round's config. |
| [harness/reach.py](harness/reach.py), [harness/reach_specs.json](harness/reach_specs.json) | The reach check and its points. |
| [harness/descriptive.py](harness/descriptive.py) | Drift of the control arm, the reach groups' differences, and uptake. |
| [REACH.md](REACH.md) | The reach check's results. |
| `results/reach/<sha>/` | Each reach run's records, one JSON file per bug. |
