# Does Peeky help where reading fails? Round 3

Rounds 1 and 2 ([../value-study](../value-study/README.md), [../value-study-2](../value-study-2/README.md))
hit a ceiling. About 9 bugs in 10 fell to reading the code, with or without Peeky. Round 2's
[report](../value-study-2/REPORT.md#what-to-do-next) asked for bugs that control solves less than
half the time, picked for runtime-only state, slow builds or hard-to-reach paths, and all
reachable by Peeky. This round finds such bugs, then compares `control` with `peeky-first` on
them.

This protocol is fixed before any agent run. The selection rule was committed first (`f09a4478`),
before any candidate was built. Changes after this commit go under [Deviations](#deviations),
with the reason.

## The design

1. **Selection:** 68 candidates from public benchmarks, hardest first, none from rounds 1 and 2.
2. **The no-LLM filter:** each candidate's image is built and its grading checked, in the control
   image and in the Peeky image. Then a script checks that Peeky stops at the fix's code and reads
   values there. No agent and no model.
3. **Screening:** the control arm alone, 1 to 3 attempts per survivor. It keeps the bugs that
   control mostly fails. Its attempts never count in the main analysis.
4. **The main run:** the kept bugs, `control` and `peeky-first`, with fresh attempts in both arms.

Peeky's commit isn't fixed yet. The Peeky arms run on the `main` that has ADR 0009 (a one-command
launcher from the project's test command, and a short first-probe doc), once it merges. The
filter's first pass used `3e51ccd7`, the `main` of 2 November. The reach check runs again on the
final commit before screening, and that run decides.

## Selection

[select_candidates.py](select_candidates.py) fixes the order, with seed 20261102. The lists are in
[candidates/](candidates/).

- **Pools.** Python: SWE-bench Verified. C, C++, Java and Rust: SWE-bench Multilingual and
  Multi-SWE-bench, one record per instance. That is 1,407 instances. Rounds 1 and 2's 29 bugs are
  left out.
- **Public difficulty.** Each benchmark's frontier is the top quarter of its public submissions by
  instances resolved, at least 8 of them. An instance's public rate is the share of that frontier
  that resolved it. The submissions are SWE-bench's (175 for Verified, 14 for Multilingual) and
  Multi-SWE-bench's (38 for C, 38 for C++, 41 for Java, 36 for Rust), fetched on 2 November.
- **Eligible:**
  - hard: a public rate of at most 0.5 (526 instances are above it);
  - solvable: at least one public submission resolved it (350 have none).
- **Tiers.** Tier 1 is a public rate of at most 0.125; tier 2 is the rest. Tier 1 comes first.
  This model is much stronger than most public submissions. On round 1's 13 bugs at 0.5 or below,
  round 2's control solved 31 of 39 attempts. On the 4 at 0.125 or below, it solved 7 of 12.
- **Runtime tag.** A candidate whose problem statement names runtime state is tagged. The word
  lists cover values in loops, concurrency and ordering, serialization, numbers, and state across
  calls. Within a tier, tagged candidates come first.
- **Order.** Within a language, tier and tag, repositories are shuffled, then taken round-robin.
- **Quotas:** Python 20, Java 16, C 8, C++ 8, Rust 16, about twice round 1's mix. The rest of
  each language's order is its reserve, in order.

| Language | Eligible | Tier 1 | Tagged | Pool |
| --- | --- | --- | --- | --- |
| Python | 86 | 44 | 34 | 20 |
| Java | 48 | 18 | 29 | 16 |
| C | 71 | 49 | 25 | 8 |
| C++ | 52 | 21 | 27 | 8 |
| Rust | 75 | 31 | 28 | 16 |

Slow builds and hard-to-reach paths can't be read from a problem statement. So each built bug
records them as traits in its bug.yaml (`runtime-state`, `slow-build`, `hard-to-reach`), from the
problem statement and the fix, before screening. They are for exploration only.

The tiers were added after a first draft of the rule, before it was committed and before any
candidate was built, once rounds 1 and 2's rates showed tier 2 is mostly solved.

## The no-LLM filter

Each candidate goes through three steps, in order. Agents that build environments do the work,
by a fixed brief. Every outcome is logged in `candidates/log-<agent>.md`, and a dropped candidate's
directory keeps a `dropped.txt`.

1. **Control image and grading,** as in round 1. The project builds on the study's base image
   (Ubuntu 24.04, arm64) and runs its tests with no network. The failing tests fail at the base
   commit, and pass with the upstream fix, on two runs. Grading takes at most 20 minutes. Python
   runs on CPython 3.11 or 3.12, which Peeky needs.
2. **Peeky image and its grading.** Round 2's Peeky image, with each bug's own steps: a C or C++
   project built with clang 18 at `-O0` (bug/peeky.sh), and a Rust workspace's Peeky build
   (bug/rust.json, by round 2's rule). The upstream fix is solved in it, and an unchanged checkout
   isn't.
3. **The reach check,** round 2's [reach.py](../value-study-2/harness/reach.py), with each bug's
   points in its `reach.json`, by round 2's rules. A candidate is **reached** when a gold point
   stopped and `session inspect` read at least one value there.

Rules for the reach check, beyond round 2's:

- **The run.** Each point runs one of the bug's failing tests, as the hidden tests run it at the
  base commit. A gold point uses the first failing test, in grade.json's order, that runs its
  line. A failing test that can't build or run at base can't reach anything, and its candidate
  is dropped as soon as that is known.
- **Corrections.** The points are fixed before the first run and never move. The run command may
  be corrected when it didn't run the failing test, or as a Peeky message says (such as "use
  --run with a script"). Every correction is logged, and the first run's record is kept.
- **No workarounds.** A Peeky refusal is recorded as it is. reach.py retries what it retried in
  round 2: a retryable refusal, "build again", and the lockfile remedy.
- **The deciding run** is on the final Peeky commit, before screening, for every candidate that
  passed steps 1 and 2. A candidate reached only on `3e51ccd7` is dropped; one reached only on the
  final commit survives.

[FILTER.md](FILTER.md) has the results of the first pass, and the rulings made after it, before
any agent run.

**Refill.** Screening keeps perhaps a third of the survivors (below), and the main run needs at
least 15 bugs. So if fewer than 50 candidates survive on the final commit, reserves are examined
by the same steps, before screening, until 50 survive or 40 reserves are examined. They are taken
one at a time in this order of languages: Python, Java, Python, Java, C, Rust, and again. C++ is
skipped while none of its 8 candidates is reached. Within a language, reserves are taken in their
order. This rule was set after the first pass, from its yields, before any agent run. The order of
examination is [candidates/refill.txt](candidates/refill.txt). Several reserves are examined at
once, so survivors are counted in that order: the refill ends at the reserve that brings the
total to 50, and any examined after it don't count.

## Screening

The control arm alone, on the survivors, with round 1's prompt, image and budgets. It runs through
round 1's harness with [study-screening.json](study-screening.json), and records go to
`results/screening/`.

- **Wave 1:** one attempt per survivor, in an order shuffled with seed 20261102, at most 3 at
  once.
- **Wave 2:** two more attempts for each survivor whose first attempt failed.
- **Kept:** the first attempt failed, and the second and third solved at most once between
  them. So a kept bug was solved at most once in three.
- An infrastructure failure is retried, as in round 1, and never counts.
- [harness/screen.py](harness/screen.py) applies the rule and writes `results/screening/screening.json`.

The chance that a bug is kept is (1 − p)²(1 + p), where p is its true control rate:

| p | 0.1 | 0.2 | 0.3 | 0.5 | 0.7 | 0.9 |
| --- | --- | --- | --- | --- | --- | --- |
| kept | 0.89 | 0.77 | 0.64 | 0.38 | 0.15 | 0.02 |

How many are kept depends on the pool's true rates. Of 50 survivors, about 21 would be kept if
the rates were uniform, 12 if the pool is one this model mostly solves (Beta(2, 1)), and 30 if it
mostly fails it (Beta(1, 2)). Rounds 1 and 2 point to the lower end.

**Screening attempts don't count** in the main analysis, in either arm. The main run's control
attempts are fresh (see [Threats](#threats-from-selecting-on-control-failure)).

**How many go on.**

- At least 12 kept: the main run takes every kept bug, or 30 if more are kept. Those 30 are the
  first 30 of the kept list, shuffled with seed 20261102.
- Fewer than 12 kept: the coordinator stops and reports, and the owner decides. Screening more
  reserves would be a deviation.

## The main run

- **Arms:** `control` and `peeky-first`.
- **`peeky` is dropped.** With round 1's "use it if it helps", agents ran Peeky in 5 of 87
  attempts in round 2. That arm measures uptake, which is now known, and a third arm costs half
  again as many attempts.
- **Agent:** GitHub Copilot CLI with the shell as its only tool, as before. Round 2 ran 1.0.105,
  and the organization's policy set the model, `gpt-6.1-sol`. Each attempt records both.
- **Images:** `control` uses the bug's control image. `peeky-first` uses `peeky-r3`, round 2's
  Peeky image on the final Peeky commit, with the bug's own steps. Its differences from the control
  image are round 2's: Peeky, its docs and PROJECT.md; a C or C++ build for Peeky; Rust's
  vendored crates and one Peeky build.
- **Prompts:** both arms get round 1's [prompt.md](../value-study/harness/prompt.md).
  `peeky-first` adds round 2's [prompt-peeky-first.md](../value-study-2/prompts/prompt-peeky-first.md),
  word for word, with one possible change. If ADR 0009 adds a short first-probe doc, the sentence
  "Start with `/opt/peeky/docs/AGENT_GUIDE.md`" names that doc instead. This is fixed before
  screening, and recorded here.
- **Attempts:** k fresh attempts per bug in each arm, with k = 160 / bugs rounded up, at least 6
  and at most 11. So 12 to 15 bugs get 11, 20 get 8, 25 get 7, and 30 get 6.
- **Order:** all attempts shuffled with seed 20261102, at most 3 at once.
- **Budgets:** 40 minutes and 250 AI credits per attempt, as in rounds 1 and 2.
- **Pilot:** one attempt per arm on the first kept bug in the main run's order, to check the
  harness. It doesn't count, and its records stay outside `results/`.

## Outcomes

- **Primary:** the paired difference in solve rate, `peeky-first` − `control`. It is the mean
  over bugs of each bug's rate in one arm minus its rate in the other. Its 95% interval resamples
  bugs, 10,000 times, as in rounds 1 and 2.
- **How it is read.** An improvement is detected when the interval's lower end is above zero.
  Otherwise the report says none was detected, gives the interval, and claims no equivalence.
- **Secondary,** as in round 2:
  - Peeky commands per attempt, the attempts that ran Peeky, and program values Peeky printed;
  - AI credits, wall time and tool calls per bug, as paired median differences;
  - wrong root causes, by round 1's blind judge, on every attempt;
  - values Peeky printed that the program contradicts, by round 2's second judge.
- **Exploration:** by language, and by trait (`runtime-state`, `slow-build`, `hard-to-reach`).
  Also the main run's control rate against the screening's, to show the regression to the mean.
- **Better measures.** Round 2 asked to count Peeky runs inside scripts, and to take values from
  Peeky's own records. If that lands in the harness before screening, both are used, and listed.
  They don't change the primary outcome.

## Power

[harness/power.py](harness/power.py) simulates the study, by these steps:

1. Each bug's true control rate p is drawn from a prior. It is then kept with the screening's
   chance, (1 − p)²(1 + p).
2. `peeky-first`'s rate is p plus the improvement d, plus the bug's own deviation, with standard
   deviation 0.08. Rounds 1 and 2 put that between 0 and 0.08: their bugs' differences were no
   wider than chance.
3. Each arm gets k fresh attempts per bug. The analysis is round 1's bootstrap.

The table gives the smallest d detected with power 0.8, on a 2.5-point grid, with 400 simulated
studies per cell. "Uniform" assumes nothing about the pool. "Hard" is Beta(1, 2), a pool this
model mostly fails. "Easy" is Beta(2, 1), a pool it mostly solves, as rounds 1 and 2 suggest.

| Bugs | Attempts per arm | Uniform | Hard | Easy |
| --- | --- | --- | --- | --- |
| 12 | 11 | 17.5 | 17.5 | 17.5 |
| 15 | 11 | 15 | 12.5 | 15 |
| 20 | 8 | 15 | 15 | 17.5 |
| 25 | 7 | 15 | 15 | 15 |
| 30 | 6 | 15 | 12.5 | 15 |

- With every design the rule allows, the study can detect an improvement of about 15 points
  (12.5 to 17.5). A 5- or 10-point gain would likely go undetected. With 400 studies per cell,
  neighbouring cells differ by chance by one step.
- Attempts per bug can stand in for bugs, because bugs varied little beyond chance in rounds 1
  and 2. If kept bugs vary more, with a deviation of 0.15, the smallest d detected is 15 to 20
  points (`--tau 0.15`).
- For scale: round 2's interval spanned 10 points, with 29 bugs at rates near 0.9. Rates near 0.4
  vary three times as much per attempt, hence more attempts per bug.

## Analysis

`harness/study.sh analyze.py` with [study.json](study.json): round 1's analysis, with this round's
two arms and seed. Screening is described apart, with `harness/screen.py status`.

## Exclusions

- An infrastructure failure is retried at most twice, as in round 1. If it still fails, the
  attempt is excluded and listed.
- A control attempt that ran Peeky is contaminated and excluded.
- A `peeky-first` attempt that didn't run Peeky counts as it is. The comparison is of prompts.
- No kept bug is dropped once the main run starts. A kept bug whose images fail their check before
  the main run is left out of both arms, and listed.

## Seeds

20261102 for everything: the selection's order, the screening's and the main run's orders, which
30 bugs go on if more are kept, the bootstrap, and the judges' order.

## Threats from selecting on control failure

- **Regression to the mean.** Some kept bugs failed in screening by bad luck. Their fresh control
  rate will be higher than in screening. Both main-run arms are fresh, so the paired comparison is
  fair. Comparing `peeky-first` with the screening's control rate would flatter Peeky, and the
  report never does it.
- **The same model chose the bugs.** The bugs are those this model and CLI usually fail. The result
  speaks to them, not to hard bugs in general. If the model or the CLI changes between screening
  and the main run, the selection no longer means the same: the run pauses and the change is
  reported.
- **Time between screening and the main run.** Drift in the CLI or its service can move both arms.
  The main run starts as soon as screening ends.
- **Bugs no agent can solve from the prompt.** Selecting on failure favors them: hidden tests that
  check behavior the report doesn't state. No tool helps there, so they pull any effect toward
  zero. The "solvable" rule limits this. The report lists the kept bugs that no attempt solved.
- **Only bugs Peeky reaches.** The result says nothing of bugs it can't reach. The filter's
  results show how many that is.
- **The languages are uneven.** The reach check, not the rule, sets the mix. After the first
  pass, the 34 survivors are Python 16, Java 10, C 3, C++ 0 and Rust 5 ([FILTER.md](FILTER.md)),
  and the refill favours Python and Java the same way. So the result speaks mostly to Python and
  Java, nothing to C++, and little to C and Rust. The report gives the final split, and results
  by language are exploration only.
- **A floor.** If both arms solve almost none of the kept bugs, the study has little power.
- **Weak difficulty signals.** Multi-SWE-bench's submissions are older and weaker than this
  model, so its public rates sort difficulty poorly. The runtime tag is a crude word list.
- **Images differ** between the arms, as in round 2: Peeky's builds of C, C++ and Rust.
- **One look.** The analysis runs once, after every attempt.

## Cost and time estimate

From round 2: control attempts cost 21.7 AI credits on average, and `peeky-first` 32.0. On harder
bugs they cost more: a failed control attempt cost 31.3, and on the 8 costliest bugs, control
cost 35.3 and `peeky-first` 48.5. So this estimate takes 35 (25 to 50) for control and 46 (35 to
60) for `peeky-first`. Wall time per attempt is about 330 s and 420 s, at 3 at once.

| Stage | Attempts | AI credits | Wall time |
| --- | --- | --- | --- |
| The filter | none | none | 0 (agents' time, not Copilot CLI) |
| Screening, ~50 survivors | ~100 (50 + 2 × 25; 80 to 120) | ~3,500 (2,000 to 6,000) | ~3.5 h (3 to 4.5) |
| Main run, 12 to 30 bugs | 264 to 360 | ~13,000 (7,900 to 19,800) | ~9 to 13 h |
| Pilot | 2 | ~80 | 0.2 h |

- Screening: the second wave runs for the first attempts that fail, assumed half (30% to 70%).
- Main run: 160 to 180 attempts per arm (132 with 12 bugs), at 35 and 46 credits.
- The refill to 50 survivors costs no AI credits: about 30 reserves, and 5 to 8 hours of
  building.
- The per-attempt cap bounds the worst case at 250 credits each.
- Not counted: the judges, as in rounds 1 and 2.

## Reproducing

```sh
# Candidates (data: SWE-bench and Multi-SWE-bench, and their public results).
select_candidates.py <data dir> <public results.json> candidates
# The filter, per candidate: control image, then:
harness/study.sh grade.py <bug> [--gold] [--name gold-2] --write-validation
PEEKY_CHECKOUT=<peeky checkout> harness/build_images.sh <bug>
harness/study.sh run_one.py <bug> peeky-first 1 --fake-agent gold|noop --results <scratch>
harness/reach.sh <bug>
# The deciding reach run, then screening.
harness/screen.py survivors results/reach/<sha, 12>
harness/screen.sh wave1 && harness/screen.sh wave2 && harness/screen.py status --write
# The main run.
harness/study.sh schedule.py --reps <k> --only <kept bugs> --concurrency 3 --budget-minutes 40 --budget-credits 250
harness/study.sh analyze.py
```

Every Docker command of the filter ran through [harness/heavy.py](harness/heavy.py), which holds
one of a few machine-wide slots (3 in the first pass), each limited to 4 CPUs and 6 GB.

## Deviations

- **A round 2 bug in the reserve.** The rule leaves out rounds 1 and 2's bugs by their ids, compared
  with case. Multi-SWE-bench writes ripgrep-2209 as `BurntSushi__ripgrep-2209`, and round 1 as
  `burntsushi__ripgrep-2209`, so it stayed in the Rust reserve, 6th. It is skipped in the refill,
  and the next Rust reserve takes its place. Nothing else matches a round 1 or 2 bug, with or
  without case. The pool and the order are otherwise as committed.

## Files

| File | What it is |
| --- | --- |
| [select_candidates.py](select_candidates.py), [candidates/](candidates/) | The selection rule, the pool, the reserves, and the filter's logs. |
| [study.json](study.json), [study-screening.json](study-screening.json) | The main run's and the screening's configs. |
| `bugs/<id>/` | Each candidate's files, as in round 1, plus `reach.json`, `peeky.sh` or `rust.json`, and `dropped.txt`. |
| [FILTER.md](FILTER.md) | The filter's results. |
| [harness/](harness/) | Wrappers that run rounds 1 and 2's harness with this round's config, the screening rule, and the power simulation. |
| `results/reach/<sha>/` | Each reach run's records. |
| `results/screening/`, `results/runs/` | The screening's and the main run's attempts. |
