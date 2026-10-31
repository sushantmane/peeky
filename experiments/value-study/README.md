# Does Peeky change the outcome? A study on real bugs

This directory holds the study that issue #441 asks for. The study sets real bugs before a coding
agent twice: once with Peeky available, and once without it. Everything needed to rerun it is
here: the bug list, how it was chosen, the images, the harness and the analysis.

The protocol below was fixed before any agent run. Changes after that are listed under
"Deviations", with the reason.

## Bugs

- **Pools.** Python bugs come from SWE-bench Verified. C, C++, Java and Rust bugs come from
  SWE-bench Multilingual. Each bug is a merged fix in a public open-source project, with a problem
  statement (the original issue), the base commit, the fix, and tests that fail before the fix
  and pass after it.
- **Order.** `select_candidates.py` fixes the order in which candidates are examined, with seed
  441. Within a language, repositories and their bugs are shuffled, then taken round-robin, so no
  one repository fills a language. The order is in `candidates/`.
- **Quotas.** 30 bugs: Python 9, Java 7, C 4, C++ 4, Rust 6. Six of them are controls: two in
  Python and one in each other language.
- **Examining a candidate.** Candidates are taken in order. A candidate is skipped, with the
  reason logged in `candidates/<language>-log.md`, when it isn't eligible or its category's slots
  are full.
- **Eligibility.**
  - The project builds and runs its tests in the study's image (Ubuntu 24.04 on arm64, with the
    toolchains in `docker/base.Dockerfile`), with no network once built.
  - The hidden tests fail at the base commit and pass with the upstream fix. The tests that
    passed before still pass. Both hold on two runs, so flaky tests are out.
  - Grading takes at most 20 minutes.
  - Python projects run on CPython 3.11 or 3.12.
- **Categories.** Each bug gets one primary category from its problem statement and upstream fix,
  by these definitions. Peeky's abilities play no part.
  - **A, slow build or long test setup:** building the project and running the relevant tests
    takes 3 minutes or more in the image, or needs a large multi-module build.
  - **B, root cause in a dependency, the standard library or vendored code:** understanding the
    failure needs the behaviour of code outside the project's own sources.
  - **C, native code with complex data:** C, C++ or Rust where the faulty data lives in non-trivial
    structures (standard containers, nested structs or enums), or the build is optimized.
  - **D, hard-to-reach state:** the bug needs a particular sequence of operations, configuration,
    concurrency or deep state to show.
  - **E, control:** the cause is clear from the report and a reading of the code, with simple
    state.
  - Control slots take E bugs, the other slots take A to D bugs.

The selected list is in `bugs/`, one directory per bug, and summarised in `BUGS.md`.

## Arms

The two arms differ only in Peeky.

- **Agent:** GitHub Copilot CLI (`copilot -p`), with the same version, model, prompt template and
  budgets in both arms. The model is set by the organization's policy, and each run records it.
- **Tools:** the shell only (`bash` and its async helpers). There are no file-editing tools, no
  web tools, no MCP servers and no custom instructions. Every shell command runs inside the run's
  container: `BASH_ENV` hands the command to `docker exec`. Files are read and changed with shell
  commands.
- **Container:** a fresh one per attempt, from the bug's image.
  - The network is off (`--network none`).
  - Both arms have `SYS_PTRACE` and an unconfined seccomp profile, so any debugger works.
  - Each container gets 6 GB of memory and 4 CPUs.
  - The project's own tests and the usual debuggers are in both arms: pdb, jdb, gdb and lldb.
- **Peeky arm:** the image also holds Peeky, built from this repository at the commit recorded
  in `results/`. Its docs are in `/opt/peeky/docs`, and the prompt adds one paragraph that
  mentions it.
- **What the agent gets:**
  - the problem statement and the repository at the base commit, with history up to it and
    nothing later;
  - how to run the project's tests.

  It never sees the fix or the hidden tests.
- **Budgets:** 40 minutes of wall time and a soft cap of 250 AI credits per attempt. The pilot
  (below) may change these before the main runs, the same for both arms.
- **Repeats:** 3 attempts per bug per arm, 180 in all. The order of all attempts is shuffled
  with seed 441, and at most 3 run at once.

The prompt template is `harness/prompt.md`. The Peeky arm's paragraph is
`harness/prompt-peeky.md`.

## Grading

After the agent stops, the harness grades the attempt in the same container:

1. It restores the test files the hidden tests touch to the base commit.
2. It applies the hidden test patch.
3. It runs the bug's test command.

An attempt is **solved** when every hidden failing test now passes, and every test that passed
with the upstream fix still passes.

## Measures

Each attempt records:

- solved;
- wall time;
- AI credits and tokens (from the CLI's usage file);
- tool calls, and runs of the project's build or tests;
- whether the agent stated a wrong root cause along the way. A judge reads the agent's messages,
  without knowing the arm, against the bug's reference root cause (`root_cause` in `bug.yaml`,
  written from the upstream fix).

In the Peeky arm, each attempt also records the Peeky commands it ran, and the values Peeky
withheld. A value Peeky showed is checked for correctness where the judge or a spot check can
tell.

## Pilot

The pilot runs one bug in each arm once, to check the harness and the budgets. Its attempts don't
count toward the results.

## Analysis

`harness/analyze.py` reads `results/` and writes the report tables:

- per bug and arm: attempts solved, median time and median credits;
- per arm: the solve rate with a 95% bootstrap interval (resampling bugs), and the paired
  per-bug difference;
- the same by category and by language;
- Peeky's commands and withheld values.

The report is `REPORT.md`.

## Reproducing

1. Build the images: `docker/base.Dockerfile`, each bug's `Dockerfile`, then
   `harness/build_peeky_images.sh`.
2. Run the attempts: `harness/schedule.py`. It needs Docker and a signed-in Copilot CLI.
3. Analyse them: `harness/analyze.py`.

The study is opt-in. It isn't part of CI, and CI makes no model calls.

## Deviations

All of these were decided before any agent run.

- **29 bugs, not 30.** The C++ pool (12 bugs, 11 of them fmt) ran out with 3 accepted. The other 9
  were category E after the C++ control slot had filled. No pool was added after the fact.
- **Category A as a tag.** The examiners measured A by time only, and no bug took 3 minutes. Per
  the definition, a large multi-module build also counts. So A is recorded as also applying to
  the Lucene and Druid bugs, and their primary category stays as examined. The selection is
  unchanged.
- **Environment adaptations.** Some bugs' environments differ from the SWE-bench spec, to run on
  arm64 or Python 3.11 and 3.12. Examples are pandas 1.5.3 for seaborn, a Cargo.lock for tokio and
  axum, and Ubuntu's `ant-optional` for Lombok. Each change is recorded in that bug's
  `bug.yaml`. None of them changes a hidden test's outcome with or without the fix.

After the runs:

- **Judging.** The blind judges split, 4 to 2, on one claim made in all six of
  `sphinx-doc__sphinx-10614`'s attempts: that Graphviz's SVG path rewriting is part of the
  cause. Every one of those attempts changed `sphinx/ext/graphviz.py` too. So all six are counted
  as stating a wrong root cause, and [REPORT.md](REPORT.md) gives the counts both ways.
- **Formatting.** The harness was formatted with ruff after the runs. Nothing it does changed:
  the analysis output is identical.
