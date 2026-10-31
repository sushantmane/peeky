# Does Peeky change the outcome? Report

Issue #441. The protocol is [README.md](README.md) and the bugs are [BUGS.md](BUGS.md). Both were
fixed before any run. The data behind every number is in `results/`.

## Conclusion

**In this study, having Peeky available made no measurable difference to whether an agent fixed a
real bug, and it cost a little more.**

- **Outcome:** both arms solved about 9 bugs in 10. The paired difference, Peeky minus control,
  is −2 percentage points, with a 95% interval of −8 to +2.
- **No category stood out:** every category's and every language's interval includes zero.
- **Agents almost never used Peeky:**
  - Every Peeky-arm attempt read Peeky's guide, a median of 3 commands, but only 3 of 87 ran
    `peeky` at all.
  - Two of those ran `peeky --help`. The third tried `peeky describe` on a Java project and was
    refused with a misleading error.
  - No Peeky command printed a program value in any attempt.
- **Cost:** the Peeky arm used 1.2 more AI credits per bug (median; 95% interval +0.5 to +1.8),
  more in 23 of 29 bugs. That is the cost of reading the docs. Wall time and tool calls were the
  same.
- **Wrong root causes:** they were equally rare, 4 of 87 attempts in the control arm and 3 of 87
  in the Peeky arm.

So the study can't name a category where Peeky helps. The reason is not that Peeky's readings
were wrong or useless. The agent never got that far: it judged reading the code, a short script
and the tests enough, and on these bugs it usually was.

## Setup in brief

- **Bugs:** 29 bugs from public projects: Python 9, Java 7, C 4, C++ 3 and Rust 6. Six are
  controls. They come from SWE-bench Verified and SWE-bench Multilingual, examined in a seeded
  order.
- **Arms:** Copilot CLI 1.0.105 with model `gpt-6.1-sol` (set by the organization's policy),
  with the shell as its only tool. Each attempt ran in a fresh container with no network. The
  Peeky arm adds Peeky (`main` at `360b3f17`), its docs, and one sentence in the prompt.
- **Budgets and repeats:** 40 minutes and 250 AI credits per attempt, 3 attempts per bug per arm,
  174 attempts in shuffled order.
- **Grading:** SWE-bench style. A blind judge read each attempt's messages for wrong root causes.
- **Infrastructure:** no attempt failed for infrastructure reasons, timed out, or used Peeky in
  the control arm.
- **Totals:** the 174 attempts took 6.0 agent-hours and 3,716 AI credits.

## Results

### Per arm, and paired by bug

The per-arm table counts attempts. The paired tables compare each bug's own rates in the two
arms, and the intervals resample bugs.

#### Per arm

| Arm | Attempts | Solved | Solve rate (95% CI) | Wall s: median [IQR] | Wall s: mean ± sd | Credits: median [IQR] | Credits: mean ± sd | Timed out |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| control | 87 | 77 | 89% (78%–97%) | 111 [77–150] | 127 ± 84 | 18.2 [12.3–25.1] | 21.0 ± 13.2 | 0 |
| peeky | 87 | 75 | 86% (74%–97%) | 111 [80–150] | 123 ± 63 | 20.3 [14.1–27.9] | 21.7 ± 9.8 | 0 |

#### Paired difference in solve rate (Peeky − control)

| Group | Bugs | Control | Peeky | Difference (95% CI) |
| --- | --- | --- | --- | --- |
| All | 29 | 89% | 86% | -2 pp (-8 to +2) |
| Category B | 8 | 92% | 88% | -4 pp (-25 to +12) |
| Category C | 4 | 100% | 100% | +0 pp (+0 to +0) |
| Category D | 11 | 88% | 88% | +0 pp (+0 to +0) |
| Category E | 6 | 78% | 72% | -6 pp (-17 to +0) |
| c | 4 | 92% | 83% | -8 pp (-25 to +0) |
| cpp | 3 | 100% | 100% | +0 pp (+0 to +0) |
| java | 7 | 95% | 95% | +0 pp (+0 to +0) |
| python | 9 | 81% | 78% | -4 pp (-22 to +11) |
| rust | 6 | 83% | 83% | +0 pp (+0 to +0) |

#### Paired difference in cost per bug (Peeky − control, each bug's medians)

| Measure | Bugs | Median difference (95% CI) | Bugs where Peeky's arm cost more |
| --- | --- | --- | --- |
| Wall time (s) | 29 | +2.0 (-9.9 to +5.4) | 16 |
| AI credits | 29 | +1.2 (+0.5 to +1.8) | 23 |
| Tool calls | 29 | +0.0 (-1.0 to +1.0) | 12 |

#### Peeky usage (Peeky arm)

- Attempts that ran Peeky: 3/87 (3%); read its docs: 87.
- Commands: 3 (median per attempt 0). By subcommand: `--help` 2, `describe` 1.
- Withheld-value markers in Peeky's output: 7 (`not settable` 7).
- Solved when Peeky ran: 3/3; when it didn't: 72/84.

The 7 markers aren't values Peeky withheld. The harness counts markers in the output of any
command that runs `peeky`. One such command also printed a page of the Java cookbook, and that
page uses the phrase "not settable". No Peeky command printed a value at all, so none was
withheld or wrong. The 3 "ran Peeky" attempts are two `--help` runs and the one refusal
described below.

#### Wrong root causes (blind judge)

| Arm | Judged | Wrong root cause |
| --- | --- | --- |
| control | 87 | 4 (5%) |
| peeky | 87 | 3 (3%) |

Peeky printed a value the program contradicted in 0 of 0 checked attempts.

Sphinx-10614's six attempts are counted as wrong. The judges split on the same claim, so the
verdict was settled from what the attempts changed (see below). Without that settlement, the
counts are 2 in the control arm and 3 in the Peeky arm.

#### Excluded attempts

None.

### Per bug and arm

| Bug | Cat. | Lang. | Arm | Solved | Median wall (s) | Median credits | Median tool calls | Median build/test runs |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| apache__druid-15402 | D | java | control | 3/3 | 102 | 12.2 | 16 | 6 |
| apache__druid-15402 | D | java | peeky | 3/3 | 108 | 14.0 | 13 | 4 |
| apache__lucene-12022 | D | java | control | 2/3 | 173 | 29.4 | 29 | 5 |
| apache__lucene-12022 | D | java | peeky | 2/3 | 146 | 28.5 | 22 | 5 |
| apache__lucene-13704 | D | java | control | 3/3 | 118 | 17.3 | 19 | 4 |
| apache__lucene-13704 | D | java | peeky | 3/3 | 120 | 18.9 | 18 | 3 |
| astropy__astropy-13398 | B | python | control | 2/3 | 426 | 69.3 | 45 | 10 |
| astropy__astropy-13398 | B | python | peeky | 0/3 | 276 | 52.4 | 39 | 11 |
| burntsushi__ripgrep-2209 | D | rust | control | 3/3 | 114 | 21.8 | 22 | 7 |
| burntsushi__ripgrep-2209 | D | rust | peeky | 3/3 | 153 | 27.3 | 26 | 8 |
| django__django-11087 | D | python | control | 3/3 | 146 | 22.4 | 17 | 5 |
| django__django-11087 | D | python | peeky | 3/3 | 132 | 21.0 | 15 | 5 |
| fmtlib__fmt-3158 | C | cpp | control | 3/3 | 160 | 31.6 | 31 | 5 |
| fmtlib__fmt-3158 | C | cpp | peeky | 3/3 | 182 | 34.5 | 31 | 8 |
| fmtlib__fmt-3272 | C | cpp | control | 3/3 | 73 | 12.4 | 15 | 3 |
| fmtlib__fmt-3272 | C | cpp | peeky | 3/3 | 72 | 12.5 | 11 | 3 |
| jqlang__jq-2650 | E | c | control | 2/3 | 88 | 13.2 | 15 | 2 |
| jqlang__jq-2650 | E | c | peeky | 1/3 | 71 | 15.1 | 18 | 2 |
| micropython__micropython-13569 | C | c | control | 3/3 | 125 | 28.2 | 31 | 7 |
| micropython__micropython-13569 | C | c | peeky | 3/3 | 126 | 29.3 | 30 | 6 |
| mwaskom__seaborn-3069 | B | python | control | 2/3 | 156 | 24.1 | 19 | 3 |
| mwaskom__seaborn-3069 | B | python | peeky | 3/3 | 141 | 24.4 | 23 | 4 |
| mwaskom__seaborn-3187 | B | python | control | 3/3 | 93 | 15.3 | 14 | 4 |
| mwaskom__seaborn-3187 | B | python | peeky | 3/3 | 80 | 16.5 | 15 | 3 |
| nlohmann__json-4237 | E | cpp | control | 3/3 | 110 | 18.2 | 17 | 5 |
| nlohmann__json-4237 | E | cpp | peeky | 3/3 | 94 | 18.1 | 17 | 5 |
| pallets__flask-5014 | E | python | control | 3/3 | 28 | 4.8 | 10 | 3 |
| pallets__flask-5014 | E | python | peeky | 3/3 | 30 | 6.2 | 10 | 2 |
| projectlombok__lombok-3009 | E | java | control | 3/3 | 166 | 27.3 | 27 | 3 |
| projectlombok__lombok-3009 | E | java | peeky | 3/3 | 145 | 28.5 | 27 | 5 |
| projectlombok__lombok-3052 | B | java | control | 3/3 | 155 | 30.0 | 33 | 6 |
| projectlombok__lombok-3052 | B | java | peeky | 3/3 | 192 | 37.7 | 35 | 6 |
| projectlombok__lombok-3326 | B | java | control | 3/3 | 138 | 28.6 | 27 | 7 |
| projectlombok__lombok-3326 | B | java | peeky | 3/3 | 144 | 34.6 | 37 | 5 |
| psf__requests-6028 | B | python | control | 3/3 | 67 | 12.9 | 14 | 4 |
| psf__requests-6028 | B | python | peeky | 3/3 | 96 | 20.0 | 18 | 4 |
| pydata__xarray-6599 | B | python | control | 3/3 | 78 | 13.8 | 14 | 4 |
| pydata__xarray-6599 | B | python | peeky | 3/3 | 82 | 16.4 | 16 | 4 |
| reactivex__rxjava-7597 | D | java | control | 3/3 | 65 | 11.1 | 12 | 2 |
| reactivex__rxjava-7597 | D | java | peeky | 3/3 | 79 | 12.6 | 15 | 3 |
| sharkdp__bat-3108 | B | rust | control | 3/3 | 106 | 18.9 | 19 | 5 |
| sharkdp__bat-3108 | B | rust | peeky | 3/3 | 108 | 20.6 | 20 | 4 |
| sphinx-doc__sphinx-10614 | D | python | control | 0/3 | 214 | 33.3 | 21 | 8 |
| sphinx-doc__sphinx-10614 | D | python | peeky | 0/3 | 165 | 26.8 | 21 | 6 |
| sphinx-doc__sphinx-7889 | E | python | control | 3/3 | 54 | 10.6 | 15 | 4 |
| sphinx-doc__sphinx-7889 | E | python | peeky | 3/3 | 44 | 9.3 | 12 | 6 |
| tokio-rs__axum-1934 | D | rust | control | 3/3 | 135 | 21.9 | 20 | 6 |
| tokio-rs__axum-1934 | D | rust | peeky | 3/3 | 129 | 22.4 | 18 | 7 |
| tokio-rs__tokio-4867 | C | rust | control | 3/3 | 59 | 10.2 | 14 | 3 |
| tokio-rs__tokio-4867 | C | rust | peeky | 3/3 | 70 | 13.6 | 16 | 4 |
| tokio-rs__tokio-6752 | D | rust | control | 3/3 | 48 | 9.1 | 12 | 3 |
| tokio-rs__tokio-6752 | D | rust | peeky | 3/3 | 54 | 9.9 | 13 | 3 |
| uutils__coreutils-6690 | E | rust | control | 0/3 | 82 | 12.6 | 17 | 7 |
| uutils__coreutils-6690 | E | rust | peeky | 0/3 | 79 | 13.0 | 17 | 6 |
| valkey-io__valkey-1842 | D | c | control | 3/3 | 84 | 13.6 | 18 | 4 |
| valkey-io__valkey-1842 | D | c | peeky | 3/3 | 88 | 15.9 | 17 | 4 |
| valkey-io__valkey-790 | D | c | control | 3/3 | 162 | 20.6 | 20 | 5 |
| valkey-io__valkey-790 | D | c | peeky | 3/3 | 253 | 21.6 | 19 | 4 |

## Where Peeky helped, made no difference, or hurt

### Helped: nowhere observed

No attempt used a value Peeky printed, so no success can be credited to Peeky.

- **Seaborn-3069:** the Peeky arm solved 3 of 3 attempts and the control arm 2 of 3, but none of
  the Peeky-arm attempts ran Peeky.
- **Astropy-13398:** the reverse, 0 of 3 against 2 of 3, with the same caveat.

Differences like these are within the run-to-run spread the paired intervals show.

### No difference: the typical attempt

`psf__requests-6028`, Peeky arm, attempt 2: solved in 88 s for 16.9 credits. The control arm's
median for this bug was 67 s and 12.9 credits.

```text
 1  ... cat /opt/peeky/docs/AGENT_GUIDE.md
 2  rg -n 'getproxies|proxy_bypass|get_auth_from_url|Proxy-Authorization|...' requests tests
 3  sed -n '1,190p' /opt/peeky/docs/AGENT_GUIDE.md && ... rg -n 'proxy|proxies' docs
 4–11  grep and sed through requests/utils.py, adapters.py, sessions.py and the tests;
       one `python - <<'PY'` script printing what urllib3 parses from a proxy URL
12–15  edit utils.py and add tests with small Python scripts; run pytest
Final: "Proxy URL normalization dropped credentials because urllib3's `netloc` excludes
authentication information, causing 407 responses."
```

The agent read the guide twice, then never came back to it. The one runtime fact it needed, what
urllib3's `parse_url` returns, took a three-line print.

Both arms did this throughout: 160 such one-off scripts in the control arm and 142 in the Peeky
arm. Debuggers were close to absent:

- **Control arm:** no pdb, gdb, lldb or jdb.
- **Peeky arm:** four attempts checked for a debugger with `command -v gdb`, `lldb-dap` or `jdb`.
  One of them scripted a short jdb run. Another started a JVM with JDWP for Peeky to attach to,
  as described below.

### Hurt: the cost of the docs, and one misleading refusal

- **Docs:** every Peeky-arm attempt spent 2 to 7 commands on Peeky's docs. That is the +1.2
  credits per bug above. It bought nothing in these attempts.
- **`projectlombok__lombok-3326`, Peeky arm, attempt 3:** solved in 144 s for 34.9 credits,
  against a control median of 138 s and 28.6.
  1. The agent read the Java cookbook's launch and attach sections (command 20).
  2. It wrote a Peeky config for a JVM it had started with JDWP (command 23).
  3. It ran
     `PATH=/usr/lib/jvm/java-17-openjdk-arm64/bin:$PATH peeky describe /testbed --config build/utility-peeky.json`.
  4. Peeky answered `[INVALID_REQUEST] Adapter command does not match the trusted Java bundle
     layout`, with the hint "Fix the request arguments".

  The project builds on JDK 11. Peeky's Java adapter needs JDK 17 and accepts only a `java`
  named in the config. The message says neither, so the agent gave up on Peeky and went back to
  `javap` and reading the code. This is a Peeky usability bug; it is filed separately.

### Same outcome in both arms: where the agents failed

Three bugs went unsolved in both arms. Peeky played no part in either arm, but they show what
went wrong.

- **`sphinx-doc__sphinx-10614`, 0 of 6:** every attempt changed Graphviz's SVG path rewriting as
  well as the inheritance diagram's hard-coded `../`. The rewriting was correct, so all six
  stated part of the wrong cause. The blind judges split 4 to 2 on these messages, and the
  attempts' own patches settled it. This is the one bug where seeing the computed paths at run
  time might have helped, but no attempt looked.
- **`uutils__coreutils-6690`, 0 of 6:** the report is about the zsh completion for `cp`. The
  cause is that `cp`'s paths argument isn't declared required. The hidden test checks the error
  `cp` gives with no operand, which must be clap's "required arguments were not provided". Every
  attempt changed `cp`'s argument definition and fixed the completion another way, and `cp` still
  gives its own error.
- **`jqlang__jq-2650`, 3 of 6:** the report's own examples already work at the base commit. Half
  the attempts concluded nothing was left to fix and missed the remaining `.a.[]` case.

## Why Peeky wasn't used, and what that says

- **The bugs were within reach of reading.**
  - The median attempt took under 2 minutes in both arms.
  - Most bugs were solved in all six attempts.
  - When a fact about the running program was needed, a print in a one-off script was quicker
    than setting up a probe. The prompt only said "Use it if it helps", and the agent concluded it
    wouldn't.
- **The selection didn't favour Peeky's best case.** The categories meant to favour a debugger
  turned out mild in practice. No bug's build or tests took 3 minutes, so the slow-build case,
  where avoiding an edit-rebuild-print cycle pays most, isn't in the data.
- **Peeky doesn't reach most of these projects:**
  - **C and C++:** Peeky builds the program from a directory of sources itself. It can't build
    any of the 7 C and C++ projects here, which use autotools, Make or CMake, or attach to their
    binaries.
  - **Rust:** Peeky supports only dependency-free binary crates, and all 6 Rust bugs are in
    crates with dependencies.
  - **Java:** it works through a launch or attach config, but one bug's JDK 11 build met the
    refusal above.
  - **Python:** only Python was fully in reach, on all 9 bugs.

  An agent that read the guide would have seen these limits.
- **The study measures availability, not use.** It answers "does having Peeky change the
  outcome", as asked. It doesn't measure what Peeky would do in an attempt that used it on these
  bugs.

## Threats to validity

- **One agent, one model and one prompt.** Another agent, or a prompt that asks the agent to
  debug first, may behave differently.
- **Shell-only tools.** The agent had no file viewer or editor tools, so edits were made with
  shell scripts, alike in both arms.
- **Ceiling.** With about 88% solved in both arms, there was little room for a difference.
- **Contamination.** SWE-bench bugs are public and may be in the model's training data. That
  would make reading-only fixes easier in both arms.
- **Judging.** One blind judge read each attempt, without double coding. Judges disagreed on one
  bug, settled from the attempts' patches as described. Wrong root causes were rare in both arms
  either way.
- **Environment.** The images run on arm64 and sometimes differ from SWE-bench's specs, as each
  bug's `bug.yaml` records. Every bug was validated in both arms before any run.

## What to do next

- **Fix the Java refusal.** The message should name the JDK the adapter needs and the config's
  `java` key, and a JDK 17 found on PATH could be accepted.
- **Reach real native projects.** Build from `compile_commands.json`, or attach to a binary the
  project's own build made with debug info. Support Rust crates with dependencies. Until then,
  13 of these 29 bugs are out of Peeky's reach.
- **Make the first command obvious.** Every agent read the guide, and almost none acted on it.
  The guide could open with when a probe beats a print, and with one copy-paste command per
  project type.
- **Run a second study where reading fails.** Use bugs chosen for runtime-only state and slow
  edit-rebuild cycles, or a prompt that has the agent use the debugger first, to measure
  Peeky's ceiling rather than its uptake.

## Reproducing

```sh
docker build -t peeky-study/base:1 -f docker/base.Dockerfile docker/
for b in bugs/*/; do docker build -t "peeky-study/$(basename "$b" | tr A-Z a-z):control" "$b"; done
PEEKY_CHECKOUT=<peeky checkout at 360b3f17> harness/build_peeky_images.sh
harness/schedule.py --concurrency 3 --budget-minutes 40 --budget-credits 250
python3 harness/prepare_judging.py results/runs <judge dir>   # then judge (judge_prompt.md)
harness/analyze.py
```
