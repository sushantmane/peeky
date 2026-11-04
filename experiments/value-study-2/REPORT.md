# Does Peeky change the outcome? Round 2 report

Round 2 of the study that issue #441 asked for. The protocol is [README.md](README.md), fixed
before any agent run, and the reach check is [REACH.md](REACH.md). Round 1 is
[../value-study/REPORT.md](../value-study/REPORT.md). The data behind every number is in
`results/`.

**Correction (2026-11-03):** the count of attempts that printed program values was wrong both
ways. It took text Peeky didn't print for Peeky's: a cookbook's example output that the same
shell call printed, or the project's code or a test's output printed beside `peeky … --help` or
a probe. And it missed Peeky's own output where a call ran Peeky from Python code in a
here-document, or after a quoted `;`, where its reading of the command line broke. It now counts
Peeky's value lines in the output of calls that run Peeky directly, or from inline Python or
shell code the call holds: a here-document, or `-c` code
([recount_values.py](harness/recount_values.py), round 1's `metrics.py`). It doesn't read a
script file a call runs, such as `python3 repro.py`. It reads the README's definition narrowly:
a probe's table rows, `name: type = value` lines where `peeky session` ran, and `"value"` fields
where Peeky printed JSON. A line of a Peeky doc the call read never counts, nor anything a call
that only asked for `--help` printed. `peeky-first` attempts that printed values: 74 → 67 of 87.
On reached bugs that is 58 → 60 of 60, on refused bugs 10 → 1 of 21, and on the two "other"
bugs 6 of 6, unchanged. Nine attempts no longer count, six of which had counted only doc lines.
Two valkey attempts that ran Peeky from Python now count. In `peeky`: 3 → 2 of 87, all on
reached bugs. No other number in this report changed, and the conclusions stand.

## Conclusion

**With Peeky's reach much wider, and with agents told to use it first, the study detected no
improvement. Observed solve rates were similar to control's and slightly lower, and both
intervals include zero, but they also allow a loss of up to 7 or 8 points. Using Peeky first
cost about 45% more AI credits per bug.**

- **Outcome:** about 9 bugs in 10 were solved in every arm.
  - `peeky-first` − `control`: −2 percentage points (95% interval −8 to +2).
  - `peeky` − `control`: −3 points (−7 to +0).
  - Only 4 of the 29 bugs had any difference between arms. Each interval includes zero.
- **Uptake depends on the prompt:**
  - With round 1's "use it if it helps", agents ran Peeky in 5 of 87 attempts (round 1: 3).
  - Asked to use it first, they ran it in all 87, a median of 5 commands each, and printed program
    values in 67. The judges found no value Peeky printed that the program contradicted, in 65
    attempts where one could be checked.
- **Cost:** `peeky-first` cost more in every one of the 29 bugs. Per bug, it took 8.6 more AI credits
  (+6.0 to +14.4), 88 s more (+54 to +147) and 7 more tool calls. The `peeky` arm cost about the
  same as control: +0.7 credits (−0.5 to +1.2).
- **Reach:** before any attempt, a script stopped at the fix's code and read values in 20 of the
  29 bugs, up from at most 16 in round 1. In the 20 reached bugs, `peeky-first` − `control` was
  −5 points (−13 to +0); in the 9 others, +4 (+0 to +11). Neither interval excludes zero.
- **Wrong root causes** were rare in every arm: 3 of 87 attempts in control, 1 in `peeky`, and 1
  in `peeky-first`.

When told to, agents used Peeky, and in the final messages its readings became the evidence for
the root cause, such as "Peeky showed `postPos` remained `0` across both writes". In the 65
attempts the judges could check, they found no printed value the program contradicted. That
check covers 65 of the 92 attempts that ran Peeky, and only whether a value was contradicted.
On these bugs, the control arm found the cause about 9 times in 10 by reading the code and the
tests. One interpretation, which the study didn't test, is that the readings mostly confirmed
what reading the code found, at a price.

## Setup in brief

- **Bugs:** round 1's 29 bugs, unchanged: Python 9, Java 7, C 4, C++ 3 and Rust 6.
- **Arms:** Copilot CLI 1.0.105 with model `gpt-6.1-sol`, set by the organization's policy, with
  the shell as its only tool. Each attempt ran in a fresh container with no network.
  - `control`: round 1's control image and prompt.
  - `peeky`: Peeky from `main` at `fef02c78`, and round 1's paragraph: "Use it if it helps."
  - `peeky-first`: the same image, with a paragraph asking the agent to reproduce the failure and
    read the failing state with Peeky before editing.
  - The Peeky image also holds the C and C++ projects built for Peeky, the Rust dependencies
    vendored with one Peeky build, and `setup-java` on JDK 17 ([README](README.md#the-peeky-image)).
- **Budgets and repeats:** 40 minutes and 250 AI credits per attempt. There were 3 attempts per
  bug per arm, 261 in a shuffled order (seed 20261101), 3 at a time.
- **Grading:** SWE-bench style, as in round 1. Blind judges read each attempt's messages for wrong
  root causes, and a second set checked the values Peeky printed.
- **Infrastructure:** no attempt failed for infrastructure reasons, timed out, or ran Peeky in the
  control arm. The model and the CLI were the same in every attempt.
- **Totals:** the 261 attempts took 16.7 agent-hours and 6,574 AI credits (the estimate was 5,600
  to 7,500). They ran in 5.9 hours. Attempts were slower than in round 1 in every arm, control
  too: other jobs shared the machine.

## Results

The per-arm table counts attempts. The paired tables compare each bug's own rates in two arms,
and the intervals resample bugs.

### Per arm

| Arm | Attempts | Solved | Solve rate (95% CI) | Wall s: median [IQR] | Wall s: mean ± sd | Credits: median [IQR] | Credits: mean ± sd | Timed out |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| control | 87 | 79 | 91% (79%–100%) | 179 [127–227] | 198 ± 125 | 19.5 [13.7–26.1] | 21.7 ± 12.0 | 0 |
| peeky | 87 | 76 | 87% (76%–97%) | 168 [119–227] | 193 ± 113 | 19.3 [14.5–25.4] | 21.9 ± 11.4 | 0 |
| peeky-first | 87 | 77 | 89% (77%–98%) | 273 [199–376] | 300 ± 139 | 29.5 [22.1–39.0] | 32.0 ± 13.8 | 0 |

### Paired difference in solve rate (`peeky-first` − control)

| Group | Bugs | Control | peeky-first | Difference (95% CI) |
| --- | --- | --- | --- | --- |
| All | 29 | 91% | 89% | -2 pp (-8 to +2) |
| Category B | 8 | 92% | 88% | -4 pp (-25 to +12) |
| Category C | 4 | 100% | 100% | +0 pp (+0 to +0) |
| Category D | 11 | 91% | 91% | +0 pp (+0 to +0) |
| Category E | 6 | 83% | 78% | -6 pp (-17 to +0) |
| c | 4 | 100% | 92% | -8 pp (-25 to +0) |
| cpp | 3 | 100% | 100% | +0 pp (+0 to +0) |
| java | 7 | 100% | 100% | +0 pp (+0 to +0) |
| python | 9 | 81% | 78% | -4 pp (-22 to +11) |
| rust | 6 | 83% | 83% | +0 pp (+0 to +0) |

### Paired difference in solve rate (`peeky` − control)

| Group | Bugs | Control | Peeky | Difference (95% CI) |
| --- | --- | --- | --- | --- |
| All | 29 | 91% | 87% | -3 pp (-7 to +0) |
| Category B | 8 | 92% | 88% | -4 pp (-12 to +0) |
| Category C | 4 | 100% | 100% | +0 pp (+0 to +0) |
| Category D | 11 | 91% | 91% | +0 pp (+0 to +0) |
| Category E | 6 | 83% | 72% | -11 pp (-22 to +0) |
| c | 4 | 100% | 92% | -8 pp (-25 to +0) |
| cpp | 3 | 100% | 89% | -11 pp (-33 to +0) |
| java | 7 | 100% | 100% | +0 pp (+0 to +0) |
| python | 9 | 81% | 78% | -4 pp (-11 to +0) |
| rust | 6 | 83% | 83% | +0 pp (+0 to +0) |

### By reach group

Each bug's group comes from the reach check on `fef02c78`, before any attempt
([README](README.md#reach-groups-and-the-prs-they-wait-on)). "Reached": a gold point stopped and
Peeky read a value there (20 bugs). "Refused": every gold point refused (7). "Other": neither (2).
These splits are exploration.

| Comparison | Group | Bugs | Base | Arm | Difference (95% CI) |
| --- | --- | --- | --- | --- | --- |
| peeky-first − control | reached | 20 | 95% | 90% | -5 pp (-13 to +0) |
| peeky-first − control | not reached | 9 | 81% | 85% | +4 pp (+0 to +11) |
| peeky-first − control | refused | 7 | 100% | 100% | +0 pp (+0 to +0) |
| peeky-first − control | other | 2 | 17% | 33% | +17 pp (+0 to +33) |
| peeky − control | reached | 20 | 95% | 92% | -3 pp (-8 to +0) |
| peeky − control | not reached | 9 | 81% | 78% | -4 pp (-11 to +0) |
| peeky − control | refused | 7 | 100% | 95% | -5 pp (-14 to +0) |
| peeky − control | other | 2 | 17% | 17% | +0 pp (+0 to +0) |

### Paired difference in cost per bug (`peeky-first` − control, each bug's medians)

| Measure | Bugs | Median difference (95% CI) | Bugs where the peeky-first arm cost more |
| --- | --- | --- | --- |
| Wall time (s) | 29 | +87.5 (+54.1 to +147.0) | 29 |
| AI credits | 29 | +8.6 (+6.0 to +14.4) | 29 |
| Tool calls | 29 | +7.0 (+4.0 to +9.0) | 28 |

### Paired difference in cost per bug (`peeky` − control, each bug's medians)

| Measure | Bugs | Median difference (95% CI) | Bugs where Peeky's arm cost more |
| --- | --- | --- | --- |
| Wall time (s) | 29 | -10.2 (-15.8 to +12.1) | 11 |
| AI credits | 29 | +0.7 (-0.5 to +1.2) | 17 |
| Tool calls | 29 | -1.0 (-3.0 to +0.0) | 9 |

### Peeky usage

| Arm | Attempts that ran Peeky | Commands (median per attempt) | Attempts that printed program values | Solved when Peeky ran |
| --- | --- | --- | --- | --- |
| peeky | 5 of 87 | 6 (0) | 2 | 5 of 5 |
| peeky-first | 87 of 87 | 422 (5) | 67 | 77 of 87 |

- **`peeky-first`'s commands:** `probe` 308, `describe` 66, `build` 21, `--help` 13, and 49
  session subcommand invocations (`get` 13, `start` 12, `close` 9, `continue` 7, `step` 4,
  `list` 3, `report` 1). A few more ran `sql`, `queries` and `verify`.
- **Values:** the count of program values printed is approximate (README, "Outcomes"). It counts
  Peeky's value lines in the output of each call that runs Peeky directly, or from inline Python or
  shell code in the call (a here-document, or `-c`), as the correction above reads the definition.
  It misses Peeky run from a script file (`python3 repro.py`), and output a script kept rather than
  printed. It can still count a line of the same form that another program printed beside Peeky's.
  It also counts a line whose value Peeky couldn't read (`<no readable value: …>`), but each attempt
  it counts also printed a value Peeky read. The counts of Peeky commands are unchanged: they miss
  Peeky run from scripts, and a command after a quoted `;`.
- **By reach group:**

| Arm | Group | Attempts | Ran Peeky | Printed a value |
| --- | --- | --- | --- | --- |
| peeky | reached | 60 | 3 | 2 |
| peeky | not reached | 27 | 2 | 0 |
| peeky | refused | 21 | 2 | 0 |
| peeky | other | 6 | 0 | 0 |
| peeky-first | reached | 60 | 60 | 60 |
| peeky-first | not reached | 27 | 27 | 7 |
| peeky-first | refused | 21 | 21 | 1 |
| peeky-first | other | 6 | 6 | 6 |

  By that count, 1 of 21 `peeky-first` attempts printed a value where every gold point was
  refused: a real reading elsewhere, ripgrep's `crates/printer/src/util.rs:104`, with a config of
  the agent's own.

### Wrong root causes (blind judge)

| Arm | Judged | Wrong root cause | With round 1's ruling on sphinx-10614 |
| --- | --- | --- | --- |
| control | 87 | 3 (3%) | 5 |
| peeky | 87 | 1 (1%) | 4 |
| peeky-first | 87 | 1 (1%) | 4 |

- All 9 attempts at `sphinx-doc__sphinx-10614` changed Graphviz's SVG path rewriting, as round 1's
  six did. One judge counted that claim as wrong, and eight didn't.
- Round 1 settled the same split by counting all six as wrong. The last column applies that
  ruling here.
- Two judges noted that upstream later fixed that rewriting itself
  ([sphinx#11634](https://github.com/sphinx-doc/sphinx/pull/11634)): it used the output root,
  not the page's directory, as its base. So the claim is partly true, and the judged counts are
  the better ones. Round 1's ruling counted some true claims as wrong.

### Values Peeky printed (second judge)

Peeky printed a value the program contradicted in 0 of 65 checked attempts. In 27 more attempts
(of 92 that ran Peeky), it printed no value the attempt let a judge check. Those printed only
refusals, help or `describe` listings, or only values Peeky itself marked unproven.

### Drift of the control arm

This round's control arm against round 1's, bug by bug: the mean difference is +2 points (95%
interval −2 to +7). The control rate was 91%, against round 1's 89%.

### Excluded attempts

None.

### Per bug and arm

| Bug | Cat. | Lang. | Arm | Solved | Median wall (s) | Median credits | Median tool calls | Median build/test runs |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| apache__druid-15402 | D | java | control | 3/3 | 148 | 11.7 | 14 | 4 |
| apache__druid-15402 | D | java | peeky | 3/3 | 135 | 12.1 | 13 | 3 |
| apache__druid-15402 | D | java | peeky-first | 3/3 | 274 | 29.5 | 32 | 2 |
| apache__lucene-12022 | D | java | control | 3/3 | 250 | 26.4 | 24 | 7 |
| apache__lucene-12022 | D | java | peeky | 3/3 | 232 | 27.1 | 22 | 7 |
| apache__lucene-12022 | D | java | peeky-first | 3/3 | 422 | 51.5 | 47 | 6 |
| apache__lucene-13704 | D | java | control | 3/3 | 151 | 16.2 | 18 | 3 |
| apache__lucene-13704 | D | java | peeky | 3/3 | 184 | 20.5 | 19 | 6 |
| apache__lucene-13704 | D | java | peeky-first | 3/3 | 314 | 34.7 | 35 | 4 |
| astropy__astropy-13398 | B | python | control | 1/3 | 559 | 48.6 | 36 | 10 |
| astropy__astropy-13398 | B | python | peeky | 1/3 | 602 | 64.6 | 40 | 12 |
| astropy__astropy-13398 | B | python | peeky-first | 2/3 | 562 | 54.6 | 38 | 10 |
| burntsushi__ripgrep-2209 | D | rust | control | 3/3 | 223 | 24.5 | 26 | 9 |
| burntsushi__ripgrep-2209 | D | rust | peeky | 3/3 | 210 | 25.7 | 23 | 6 |
| burntsushi__ripgrep-2209 | D | rust | peeky-first | 3/3 | 274 | 32.3 | 30 | 7 |
| django__django-11087 | D | python | control | 3/3 | 218 | 23.0 | 17 | 4 |
| django__django-11087 | D | python | peeky | 3/3 | 196 | 23.8 | 19 | 6 |
| django__django-11087 | D | python | peeky-first | 3/3 | 262 | 31.1 | 23 | 4 |
| fmtlib__fmt-3158 | C | cpp | control | 3/3 | 218 | 28.3 | 29 | 7 |
| fmtlib__fmt-3158 | C | cpp | peeky | 3/3 | 264 | 31.4 | 30 | 6 |
| fmtlib__fmt-3158 | C | cpp | peeky-first | 3/3 | 314 | 38.2 | 33 | 8 |
| fmtlib__fmt-3272 | C | cpp | control | 3/3 | 120 | 13.8 | 14 | 3 |
| fmtlib__fmt-3272 | C | cpp | peeky | 3/3 | 102 | 13.2 | 12 | 2 |
| fmtlib__fmt-3272 | C | cpp | peeky-first | 3/3 | 174 | 22.4 | 21 | 5 |
| jqlang__jq-2650 | E | c | control | 3/3 | 134 | 16.8 | 18 | 3 |
| jqlang__jq-2650 | E | c | peeky | 2/3 | 118 | 15.1 | 14 | 1 |
| jqlang__jq-2650 | E | c | peeky-first | 2/3 | 152 | 17.7 | 17 | 2 |
| micropython__micropython-13569 | C | c | control | 3/3 | 209 | 24.9 | 29 | 7 |
| micropython__micropython-13569 | C | c | peeky | 3/3 | 169 | 22.1 | 25 | 5 |
| micropython__micropython-13569 | C | c | peeky-first | 3/3 | 390 | 39.8 | 38 | 8 |
| mwaskom__seaborn-3069 | B | python | control | 3/3 | 267 | 26.7 | 23 | 5 |
| mwaskom__seaborn-3069 | B | python | peeky | 2/3 | 233 | 25.2 | 22 | 3 |
| mwaskom__seaborn-3069 | B | python | peeky-first | 1/3 | 268 | 29.9 | 24 | 4 |
| mwaskom__seaborn-3187 | B | python | control | 3/3 | 138 | 17.7 | 17 | 4 |
| mwaskom__seaborn-3187 | B | python | peeky | 3/3 | 158 | 17.5 | 16 | 3 |
| mwaskom__seaborn-3187 | B | python | peeky-first | 3/3 | 210 | 21.6 | 26 | 4 |
| nlohmann__json-4237 | E | cpp | control | 3/3 | 153 | 18.0 | 19 | 5 |
| nlohmann__json-4237 | E | cpp | peeky | 2/3 | 171 | 19.3 | 17 | 7 |
| nlohmann__json-4237 | E | cpp | peeky-first | 3/3 | 210 | 23.6 | 24 | 3 |
| pallets__flask-5014 | E | python | control | 3/3 | 49 | 5.6 | 10 | 3 |
| pallets__flask-5014 | E | python | peeky | 3/3 | 40 | 6.5 | 9 | 2 |
| pallets__flask-5014 | E | python | peeky-first | 3/3 | 68 | 9.9 | 12 | 2 |
| projectlombok__lombok-3009 | E | java | control | 3/3 | 264 | 29.2 | 30 | 4 |
| projectlombok__lombok-3009 | E | java | peeky | 3/3 | 254 | 29.1 | 25 | 6 |
| projectlombok__lombok-3009 | E | java | peeky-first | 3/3 | 468 | 55.4 | 45 | 5 |
| projectlombok__lombok-3052 | B | java | control | 3/3 | 230 | 30.0 | 30 | 6 |
| projectlombok__lombok-3052 | B | java | peeky | 3/3 | 263 | 35.6 | 31 | 5 |
| projectlombok__lombok-3052 | B | java | peeky-first | 3/3 | 437 | 54.0 | 57 | 5 |
| projectlombok__lombok-3326 | B | java | control | 3/3 | 272 | 30.5 | 33 | 9 |
| projectlombok__lombok-3326 | B | java | peeky | 3/3 | 208 | 26.7 | 29 | 11 |
| projectlombok__lombok-3326 | B | java | peeky-first | 3/3 | 365 | 51.2 | 47 | 8 |
| psf__requests-6028 | B | python | control | 3/3 | 154 | 18.1 | 20 | 4 |
| psf__requests-6028 | B | python | peeky | 3/3 | 124 | 15.4 | 16 | 4 |
| psf__requests-6028 | B | python | peeky-first | 3/3 | 205 | 24.6 | 23 | 4 |
| pydata__xarray-6599 | B | python | control | 3/3 | 131 | 15.4 | 16 | 5 |
| pydata__xarray-6599 | B | python | peeky | 3/3 | 115 | 14.5 | 13 | 4 |
| pydata__xarray-6599 | B | python | peeky-first | 3/3 | 169 | 20.5 | 18 | 2 |
| reactivex__rxjava-7597 | D | java | control | 3/3 | 100 | 11.6 | 12 | 3 |
| reactivex__rxjava-7597 | D | java | peeky | 3/3 | 109 | 12.3 | 15 | 3 |
| reactivex__rxjava-7597 | D | java | peeky-first | 3/3 | 235 | 26.5 | 30 | 3 |
| sharkdp__bat-3108 | B | rust | control | 3/3 | 208 | 22.0 | 19 | 7 |
| sharkdp__bat-3108 | B | rust | peeky | 3/3 | 197 | 20.1 | 17 | 6 |
| sharkdp__bat-3108 | B | rust | peeky-first | 3/3 | 398 | 29.5 | 28 | 5 |
| sphinx-doc__sphinx-10614 | D | python | control | 0/3 | 371 | 39.2 | 24 | 8 |
| sphinx-doc__sphinx-10614 | D | python | peeky | 0/3 | 263 | 24.9 | 20 | 5 |
| sphinx-doc__sphinx-10614 | D | python | peeky-first | 0/3 | 419 | 44.1 | 33 | 9 |
| sphinx-doc__sphinx-7889 | E | python | control | 3/3 | 127 | 16.0 | 18 | 6 |
| sphinx-doc__sphinx-7889 | E | python | peeky | 3/3 | 158 | 17.2 | 18 | 4 |
| sphinx-doc__sphinx-7889 | E | python | peeky-first | 3/3 | 183 | 20.9 | 22 | 7 |
| tokio-rs__axum-1934 | D | rust | control | 3/3 | 198 | 22.2 | 22 | 6 |
| tokio-rs__axum-1934 | D | rust | peeky | 3/3 | 161 | 17.9 | 18 | 5 |
| tokio-rs__axum-1934 | D | rust | peeky-first | 3/3 | 357 | 31.3 | 29 | 10 |
| tokio-rs__tokio-4867 | C | rust | control | 3/3 | 85 | 10.3 | 12 | 4 |
| tokio-rs__tokio-4867 | C | rust | peeky | 3/3 | 117 | 13.6 | 15 | 5 |
| tokio-rs__tokio-4867 | C | rust | peeky-first | 3/3 | 242 | 24.7 | 23 | 4 |
| tokio-rs__tokio-6752 | D | rust | control | 3/3 | 82 | 9.5 | 13 | 2 |
| tokio-rs__tokio-6752 | D | rust | peeky | 3/3 | 99 | 13.1 | 15 | 4 |
| tokio-rs__tokio-6752 | D | rust | peeky-first | 3/3 | 165 | 18.9 | 18 | 4 |
| uutils__coreutils-6690 | E | rust | control | 0/3 | 106 | 12.8 | 17 | 5 |
| uutils__coreutils-6690 | E | rust | peeky | 0/3 | 118 | 15.5 | 17 | 7 |
| uutils__coreutils-6690 | E | rust | peeky-first | 0/3 | 253 | 16.4 | 22 | 7 |
| valkey-io__valkey-1842 | D | c | control | 3/3 | 133 | 14.2 | 19 | 4 |
| valkey-io__valkey-1842 | D | c | peeky | 3/3 | 123 | 14.8 | 16 | 4 |
| valkey-io__valkey-1842 | D | c | peeky-first | 3/3 | 220 | 23.9 | 22 | 4 |
| valkey-io__valkey-790 | D | c | control | 3/3 | 259 | 21.4 | 20 | 5 |
| valkey-io__valkey-790 | D | c | peeky | 3/3 | 259 | 24.0 | 22 | 5 |
| valkey-io__valkey-790 | D | c | peeky-first | 3/3 | 570 | 38.8 | 30 | 6 |

## Where Peeky helped, made no difference, or hurt

### Helped: no reliable evidence of benefit

Only 4 bugs had different observed solve rates across arms, each by one or two attempts of three:

| Bug | control | peeky | peeky-first |
| --- | --- | --- | --- |
| astropy-13398 | 1/3 | 1/3 | 2/3 |
| jq-2650 | 3/3 | 2/3 | 2/3 |
| seaborn-3069 | 3/3 | 2/3 | 1/3 |
| json-4237 | 3/3 | 2/3 | 3/3 |

- astropy-13398 had a higher observed `peeky-first` rate: 2 of 3, against 1 of 3 in control.
- The other three were lower in at least one Peeky arm than in control.
- With 3 attempts per bug, none of these differences is more than run-to-run spread could make.
  No bug shows reliable evidence of benefit.

- **astropy-13398:** two `peeky-first` attempts were solved. Both cite Peeky: it showed a 10 km
  overhead vector becoming 10.02067845 km on the old route. The one that failed cited a similar
  reading. The control arm's solved attempt used the same idea without a debugger.
- In the other three bugs, the judges found no Peeky value the program contradicted. The misses
  came from the fixes, or from the question asked (jq, below). seaborn's misses left its y-axis
  test failing. json's change broke other conversions, in an attempt that didn't run Peeky.

### No difference: the typical `peeky-first` attempt

`apache__druid-15402`, `peeky-first`, attempt 2: solved in 256 s for 27.6 credits. The control
arm's median for this bug was 148 s and 11.7 credits, and it solved 3 of 3 too.

```text
 1–12   read the guide, PROJECT.md, the Java page and cookbook, and the cache code
13–18   start the test's JVM waiting for a debugger, on the classpath of Maven's last run;
        probe GroupByQueryQueryToolChest.java:657 with an attach config (twice with no stop)
19      the probe stops twice: `postPos = 0` at both
22–25   patch the loop and the tests; check the diff
Final: "Fixed the cache-reading loop: it never advanced `postPos`, so later post-aggregates
overwrote the first. `peeky probe` at line 657 confirmed `postPos = 0` on both writes."
```

The reading matches the upstream fix's cause. But the control arm found the same loop by reading
it, in about half the time and less than half the credits. That cost pattern holds across the
arm: `peeky-first` cost more in every bug, and most of the cost went to docs, configs and runs.

### Hurt: the cost, and one wrong question

- **Cost:** +8.6 credits and +88 s per bug, in all 29 bugs. The extra commands went to reading
  the guide and the language pages, writing attach or workspace configs, and runs that Peeky
  refused before the one that stopped.
- **`jqlang__jq-2650`, `peeky-first`, attempt 2:** the agent probed the parser on the report's
  own example, `.["media:content"].["media:credits"]`, and on a slice chain. Both already parse at
  the base commit. Peeky showed it (`yyn=91`, `yylen=5`), and the agent concluded that nothing
  was left to fix. The judge counted that as a wrong root cause. The values weren't contradicted;
  they fed a wrong conclusion, because the question missed the remaining `.a.[]` case. jq's failed
  `peeky` attempt concluded the same without running Peeky, as half of round 1's jq attempts did.
- **Values the program contradicted:** in 65 checkable attempts, the judges found none. That
  doesn't show a value never misled: jq's didn't contradict the program, and still led to a wrong
  conclusion.

### Same outcome in every arm: where the agents failed

- **`sphinx-doc__sphinx-10614`, 0 of 9:** as in round 1, every attempt also changed Graphviz's
  path rewriting, and the hidden test checks the exact HTML.
- **`uutils__coreutils-6690`, 0 of 9:** as in round 1, every attempt fixed the zsh completion
  another way, and `cp` still gives its own error with no operand.

## What the reach work bought

The reach check ([REACH.md](REACH.md)) ran the same probes with no agent, on three of this
round's Peeky commits. Round 1's own reach comes from its report, which found 13 projects out of
reach:

- Round 1 couldn't reach 13 of the 29 projects. On `fef02c78`, Peeky stops at the fix's code and
  reads values in 5 of them: jq, MicroPython, both valkey bugs and bat.
- All 7 Java bugs now read values, the JDK 11 ones too. In the `peeky` arm, the attempt that
  probed `lombok-3326`, round 1's refused JDK 11 case, read the inner class's symbol.
- Still refused, 7: fmt twice and json, whose test units carry no checksums; axum, over the
  debug-information budget (#483); and the generic Rust code of tokio twice and ripgrep, refused
  by design (#463). Each refusal says why.
- coreutils, the eighth, isn't refused: Peeky stops at its gold point, `uu_app`, but that function
  has no locals to read. So it isn't reached under the pre-registered rule.

Agents used that reach when asked: in `peeky-first`, 60 of 60 attempts on reached bugs printed
values. Without the request, 2 of 60 did.

## Threats to validity

- **One agent, one model, one bug set.** Round 2 changed the prompt, not the agent or the bugs.
- **Ceiling.** About 90% were solved in every arm, as in round 1, so there was little room to help.
  Of the bugs that failed, two failed in every arm for reasons no reading fixes (sphinx and
  coreutils, above).
- **The comparison is of prompts.** `peeky-first` asks for more work before the fix. Its cost
  measures that request as much as Peeky. A prompt that asks for any debugger first may cost the
  same.
- **Images.** The Peeky arms' images differ from control beyond Peeky. jq and valkey run their tests on
  a clang `-O0` build, and the Rust images hold a Peeky build. These are listed in the README.
  Grading rebuilds each project its own way in every arm.
- **Intervals** resample bugs from each bug's observed rate over 3 attempts, as round 1 did. A
  `+0 to +0` interval means every bug had the same observed rate in both arms. It doesn't prove the
  arms equal.
- **Measures.** The count of values printed sees Peeky run from inline code in the call (a
  here-document, or `-c`), but not from a script file. It no longer counts lines of Peeky's docs
  (correction, above), but it can still count a line of other output a call printed beside Peeky's.
  The counts of Peeky commands miss Peeky run from scripts: valkey's `peeky-first` attempts ran
  Peeky inside Python scripts, and the judges counted those. The judges' checks are the firmer
  measure.
- **Judging.** One judge read each attempt. The sphinx-10614 split is reported both ways. Two judges
  read upstream history to settle it, beyond their input.
- **Time.** Other jobs shared the machine, and attempts took longer than round 1's in every arm.
  Wall-time comparisons hold within this round, not across rounds.
- **Contamination.** SWE-bench bugs are public, and may be in the model's training data.

## What to do next

- **Measure Peeky where reading fails.** Both rounds hit a ceiling: about 9 in 10 of these bugs fall
  to reading, with or without Peeky. A third study needs bugs that control solves less than half
  the time. Pick them for runtime-only state, slow builds or hard-to-reach paths, and keep the
  reach check, so every bug is one Peeky can reach.
- **Make a Peeky question cheaper.** Most of `peeky-first`'s extra cost is set-up, not
  probing:
  - reading a 20 KB guide and a language page;
  - writing a Java attach config by hand, and starting the test with a debug flag;
  - writing a Rust workspace config;
  - probes refused before one stopped.

  A command that starts a project's own test under the debugger and writes the config would cut most
  of it. So would a shorter path through the docs for the first probe. [SETUP_COST.md](SETUP_COST.md)
  measures this set-up, call by call.
- **Close the reach gaps found here:** bat's integration test under Peeky's build (#488), axum's
  debug-information budget (#483), generic Rust code that only another crate compiles, and C++
  test units without checksums (phase 3's compiler wrapper).
- **Measure better:** count Peeky runs inside scripts, and take the values printed from Peeky's
  own records rather than its output text.
- **Revisit round 1's sphinx-10614 ruling,** given sphinx#11634.

## Reproducing

```sh
PEEKY_CHECKOUT=<peeky checkout at fef02c78> harness/build_images.sh
harness/reach.py
harness/study.sh schedule.py --concurrency 3 --budget-minutes 40 --budget-credits 250
harness/study.sh prepare_judging.py results/runs <judge dir>   # then judge (judge_prompt.md)
harness/collect_verdicts.py <judge dir>
harness/study.sh analyze.py
harness/descriptive.py --reach results/reach/fef02c78b57b
```

The judges were sub-agents. Twelve read 21 or 22 attempts each for part 1, and twelve read 7 or 8
each for part 2.
