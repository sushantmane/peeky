# Round 3's no-LLM filter: the first pass, the refill and the deciding run

The [README](README.md#the-no-llm-filter) gives the steps and rules. Both passes ran on 2 and 3
November, with Peeky at `3e51ccd7` (the `main` of those days), before ADR 0009. The deciding run,
on the final Peeky commit `1422812c`, is [below](#the-deciding-run-on-1422812c): it kept the same
50 survivors.

Eight agents built the pool's 68 candidates, and six built the refill's 40, each by a fixed brief,
with at most 3 Docker builds or containers at once. Each candidate's log row is in
`candidates/log-<agent>.md` (`log-r-*.md` for the refill), and its reach record in
`results/reach/3e51ccd7232d/`. The tables come from `harness/filter_table.py
results/reach/3e51ccd7232d`, and the survivors from `harness/screen.py survivors`.

## The deciding run on `1422812c`

On 4 November, before any agent run, the Peeky images were built again on `1422812c`, the `main`
with ADR 0009, and the reach check ran again, by the same rules and the same points. No agent and
no model.

- **The survivors, and the 3 reached after the refill's end.** Each `peeky-r3` image was rebuilt
  from its control image, one at a time, with 4 CPUs and 6 GB. Its docs now hold FIRST.md, and
  FIRST.md, like the guide, points to PROJECT.md in its third line (README, "The main run").
  Each PROJECT.md says what its image prepared, as on the first pass: the virtual environment
  (26), JDK 17 for `peeky setup-java` (17), a Peeky build of the C project (5), or the vendored
  crates and a current Peeky build of its config (5). In every image, the upstream fix solved
  and an unchanged checkout didn't ([results/image-check/1422812cef29.tsv](results/image-check/1422812cef29.tsv)).
- **The other 25 that passed steps 1 and 2.** None was reached on `3e51ccd7`, and their images
  had been removed after the drop. Each control image was built again from its Dockerfile: 8
  wholly from Docker's build cache, 17 anew, from the same pinned commits. Then its `peeky-r3`
  image was built, and the reach check ran. Steps 1 and 2 weren't run again, since none was
  reached. As on the first pass, fastjson2-1245's Peeky image fails its Java check: the project
  runs on JDK 8, where Peeky launches nothing, but the attach that the reach check uses works.
- **Not run again:** the candidates dropped because the failing test can't build at base, or
  because the fix has no gold point. Nothing can reach them.
- Records: `results/reach/1422812cef29/`. The tables come from `harness/deciding_table.py
  results/reach/1422812cef29 results/first-call/1422812cef29 results/image-check/1422812cef29.tsv`.

| Language | Rerun | Reached on `3e51ccd7` | Reached on `1422812c` | Survivors | Read in one `--test` call |
| --- | --- | --- | --- | --- | --- |
| Python | 27 | 26 | 26 | 25 | 21 |
| Java | 22 | 17 | 17 | 15 | 14 |
| C | 10 | 5 | 5 | 5 | 5 |
| C++ | 4 | 0 | 0 | 0 | 0 |
| Rust | 15 | 5 | 5 | 5 | 5 |
| **All** | **78** | **53** | **53** | **50** | **45** |

**The same 50 survive.** All 196 points of the 78 candidates, gold points and assertions, ended
as on `3e51ccd7`: the same outcome, the same error code where refused, and the same number of
values read. Peeky's changes since `3e51ccd7` are ADR 0009's: `--test`, FIRST.md, refusals that
name the next step, and Java configs that may give a path inside the root as an absolute path.
All the reach gaps of the first pass are still open (#483, #495 to #504). So the survivors are
Python 25, Java 15, C 5, C++ 0 and Rust 5, and [candidates/survivors.txt](candidates/survivors.txt)
is unchanged.

### One `--test` call

Each gold point also got one `peeky probe … --test -- <test command>`, the first question as
FIRST.md asks it ([harness/first_call.py](harness/first_call.py); README, "The no-LLM filter").
It decides nothing. **45 of the 50 survivors read a value at a fix line in one call**: Python 21
of 25, Java 14 of 15, C 5 of 5 and Rust 5 of 5. Of the 3 reached after the refill's end, 2 did.
Of the 78 gold points tried (logstash-16094's Ruby point is not attempted), 69 ended as in the
session check: a stop where it stopped, and a value read where it read one. The other 9, in 6
candidates, were refused for the test command alone:

- **sympy-17318, -21596 and -22080:** `python bin/test …` is refused, "entry must be a .py file
  inside the root", with no next step: sympy's test script has no `.py` suffix. Filed as #509.
  The reach check copies it to `bin/reach_test.py`, a correction the first pass's rulings
  allowed.
- **pytest-10356:** pytest's own checkout. `python -m pytest` there would import its sources, not
  an installed pytest, and Peeky refuses, "Rename it, or use --run". The reach check runs
  `--run src/pytest/__main__.py`, as that message says.
- **lombok-3215, and lombok-3674 (after the end):** Lombok's tests run under Ant, which `--test`
  refuses by design: Ant forks its test JVMs with no switch Peeky can add its agent to. The reach
  check edits the Ant target to start the test JVM waiting for a debugger, and attaches.

The test command is the gold point's run, written as FIRST.md writes test commands: `python -m
pytest ARGS`; the project's own script, `python tests/runtests.py ARGS` (Django) or `python
bin/test ARGS` (sympy); the Maven or Gradle command of the reach check's `launch`, less its
debug switch; `cargo test -p PKG --test T [--features F] FILTER -- ARGS`, or `cargo run --bin rg
-- ARGS` (ripgrep, whose reach check runs the binary); and a C binary with its arguments. The
captures are the names the session check read at that point. Records:
`results/first-call/1422812cef29/`.

### Every candidate the deciding run checked

| Candidate | Language | `3e51ccd7` | `1422812c` | Image check | Result | One `--test` call |
| --- | --- | --- | --- | --- | --- | --- |
| astropy__astropy-14598 | Python | reached, 10 values | reached, 10 values | gold solved, noop not solved | **survives** | read 4 of 4 |
| django__django-11820 | Python | reached, 9 values | reached, 9 values | gold solved, noop not solved | **survives** | read 4 of 4 |
| matplotlib__matplotlib-23476 | Python | reached, 2 values | reached, 2 values | gold solved, noop not solved | **survives** | read 2 of 2 |
| sympy__sympy-17318 | Python | reached, 2 values | reached, 2 values | gold solved, noop not solved | **survives** | refused (INVALID_REQUEST) |
| pylint-dev__pylint-8898 | Python | reached, 2 values | reached, 2 values | gold solved, noop not solved | **survives** | read 2 of 2 |
| pytest-dev__pytest-10356 | Python | reached, 0 values; reached, 1 values | reached, 0 values; reached, 1 values | gold solved, noop not solved | **survives** | refused (INVALID_REQUEST); refused (INVALID_REQUEST) |
| astropy__astropy-8707 | Python | reached, 2 values; no stop | reached, 2 values; no stop | gold solved, noop not solved | **survives** | read 2 of 2; no stop |
| django__django-12325 | Python | reached, 20 values; reached, 3 values | reached, 20 values; reached, 3 values | gold solved, noop not solved | **survives** | read 4 of 4; read 3 of 3 |
| matplotlib__matplotlib-26208 | Python | reached, 2 values; no stop | reached, 2 values; no stop | gold solved, noop not solved | **survives** | read 2 of 2; no stop |
| astropy__astropy-13977 | Python | reached, 5 values | reached, 5 values | gold solved, noop not solved | **survives** | read 4 of 4 |
| django__django-13195 | Python | reached, 3 values; reached, 4 values | reached, 3 values; reached, 4 values | gold solved, noop not solved | **survives** | read 3 of 3; read 4 of 4 |
| matplotlib__matplotlib-24870 | Python | reached, 2 values; no stop | reached, 2 values; no stop | gold solved, noop not solved | **survives** | read 2 of 2; no stop |
| sphinx-doc__sphinx-10435 | Python | reached, 5 values | reached, 5 values | gold solved, noop not solved | **survives** | read 4 of 4 |
| astropy__astropy-13236 | Python | reached, 9 values | reached, 9 values | gold solved, noop not solved | **survives** | read 4 of 4 |
| astropy__astropy-8872 | Python | reached, 7 values; reached, 8 values | reached, 7 values; reached, 8 values | gold solved, noop not solved | **survives** | read 4 of 4; read 4 of 4 |
| django__django-11400 | Python | reached, 4 values; reached, 4 values | reached, 4 values; reached, 4 values | gold solved, noop not solved | **survives** | read 4 of 4; read 4 of 4 |
| fasterxml__jackson-databind-4087 | Java | reached, 2 values | reached, 2 values | gold solved, noop not solved | **survives** | read 2 of 2 |
| alibaba__fastjson2-1245 | Java | no stop | no stop | — | dropped | — |
| elastic__logstash-16579 | Java | reached, 4 values; no stop | reached, 4 values; no stop | gold solved, noop not solved | **survives** | read 4 of 4; no stop |
| fasterxml__jackson-databind-3716 | Java | reached, 4 values | reached, 4 values | gold solved, noop not solved | **survives** | read 4 of 4 |
| elastic__logstash-16094 | Java | not attempted; reached, 4 values | not attempted; reached, 4 values | gold solved, noop not solved | **survives** | not attempted; read 4 of 4 |
| elastic__logstash-15928 | Java | reached, 5 values | reached, 5 values | gold solved, noop not solved | **survives** | read 4 of 4 |
| fasterxml__jackson-databind-3666 | Java | no stop; reached, 4 values | no stop; reached, 4 values | gold solved, noop not solved | **survives** | no stop; read 4 of 4 |
| fasterxml__jackson-databind-3509 | Java | reached, 3 values; no stop | reached, 3 values; no stop | gold solved, noop not solved | **survives** | read 3 of 3; no stop |
| alibaba__fastjson2-82 | Java | reached, 1 values; reached, 1 values | reached, 1 values; reached, 1 values | gold solved, noop not solved | **survives** | read 1 of 1; read 1 of 1 |
| projectlombok__lombok-3215 | Java | no stop; reached, 4 values | no stop; reached, 4 values | gold solved, noop not solved | **survives** | refused (UNSUPPORTED); refused (UNSUPPORTED) |
| apache__dubbo-7041 | Java | reached, 4 values | reached, 4 values | gold solved, noop not solved | **survives** | read 4 of 4 |
| ponylang__ponyc-3763 | C | reached, 0 values | reached, 0 values | — | dropped | — |
| facebook__zstd-836 | C | reached, 0 values; reached, 0 values | reached, 0 values; reached, 0 values | — | dropped | — |
| jqlang__jq-2840 | C | reached, 8 values | reached, 8 values | gold solved, noop not solved | **survives** | read 4 of 4 |
| ponylang__ponyc-4583 | C | reached, 1 values; reached, 3 values | reached, 1 values; reached, 3 values | gold solved, noop not solved | **survives** | read 1 of 1; read 3 of 3 |
| facebook__zstd-1530 | C | no stop; no stop | no stop; no stop | — | dropped | — |
| ponylang__ponyc-3777 | C | refused (UNSUPPORTED) | refused (UNSUPPORTED) | — | dropped | — |
| ponylang__ponyc-4057 | C | reached, 18 values | reached, 18 values | gold solved, noop not solved | **survives** | read 4 of 4 |
| nlohmann__json-2866 | C++ | refused (UNSUPPORTED); refused (UNSUPPORTED) | refused (UNSUPPORTED); refused (UNSUPPORTED) | — | dropped | — |
| fmtlib__fmt-1390 | C++ | refused (INVALID_BREAKPOINT) | refused (INVALID_BREAKPOINT) | — | dropped | — |
| catchorg__Catch2-2723 | C++ | refused (UNSUPPORTED); refused (UNSUPPORTED) | refused (UNSUPPORTED); refused (UNSUPPORTED) | — | dropped | — |
| fmtlib__fmt-3260 | C++ | refused (UNSUPPORTED); refused (UNSUPPORTED) | refused (UNSUPPORTED); refused (UNSUPPORTED) | — | dropped | — |
| nushell__nushell-10629 | Rust | refused (UNSUPPORTED) | refused (UNSUPPORTED) | — | dropped | — |
| BurntSushi__ripgrep-2295 | Rust | reached, 5 values | reached, 5 values | gold solved, noop not solved | **survives** | read 4 of 4 |
| clap-rs__clap-2161 | Rust | refused (BUILD_FAILED); refused (BUILD_FAILED) | refused (BUILD_FAILED); refused (BUILD_FAILED) | — | dropped | — |
| clap-rs__clap-2696 | Rust | refused (BUILD_FAILED) | refused (BUILD_FAILED) | — | dropped | — |
| clap-rs__clap-3391 | Rust | refused (BUILD_FAILED); refused (BUILD_FAILED) | refused (BUILD_FAILED); refused (BUILD_FAILED) | — | dropped | — |
| clap-rs__clap-5873 | Rust | reached, 7 values | reached, 7 values | gold solved, noop not solved | **survives** | read 3 of 4 |
| tokio-rs__tokio-5179 | Rust | reached, 0 values; reached, 1 values | reached, 0 values; reached, 1 values | gold solved, noop not solved | **survives** | stopped, none read; read 1 of 1 |
| nushell__nushell-11292 | Rust | refused (UNSUPPORTED); refused (UNSUPPORTED) | refused (UNSUPPORTED); refused (UNSUPPORTED) | — | dropped | — |
| clap-rs__clap-4005 | Rust | reached, 6 values | reached, 6 values | gold solved, noop not solved | **survives** | read 3 of 4 |
| BurntSushi__ripgrep-2576 | Rust | reached, 5 values | reached, 5 values | gold solved, noop not solved | **survives** | read 3 of 4 |
| matplotlib__matplotlib-23299 | Python | reached, 2 values | reached, 2 values | gold solved, noop not solved | **survives** | read 2 of 2 |
| fasterxml__jackson-core-1016 | Java | no stop; no stop | no stop; no stop | — | dropped | — |
| django__django-11734 | Python | reached, 2 values; reached, 6 values | reached, 2 values; reached, 6 values | gold solved, noop not solved | **survives** | read 2 of 2; read 4 of 4 |
| mockito__mockito-3173 | Java | reached, 5 values; reached, 2 values | reached, 5 values; reached, 2 values | gold solved, noop not solved | **survives** | read 4 of 4; read 2 of 2 |
| facebook__zstd-2130 | C | reached, 4 values; reached, 16 values | reached, 4 values; reached, 16 values | gold solved, noop not solved | **survives** | read 4 of 4; read 4 of 4 |
| nushell__nushell-13870 | Rust | refused (UNSUPPORTED); refused (UNSUPPORTED) | refused (UNSUPPORTED); refused (UNSUPPORTED) | — | dropped | — |
| elastic__logstash-16681 | Java | reached, 4 values | reached, 4 values | gold solved, noop not solved | **survives** | read 4 of 4 |
| sympy__sympy-22080 | Python | reached, 11 values | reached, 11 values | gold solved, noop not solved | **survives** | refused (INVALID_REQUEST) |
| ponylang__ponyc-4017 | C | reached, 5 values | reached, 5 values | gold solved, noop not solved | **survives** | read 4 of 4 |
| tokio-rs__tracing-2001 | Rust | reached, 0 values; refused (INVALID_BREAKPOINT) | reached, 0 values; refused (INVALID_BREAKPOINT) | — | dropped | — |
| scikit-learn__scikit-learn-26194 | Python | reached, 9 values | reached, 9 values | gold solved, noop not solved | **survives** | read 4 of 4 |
| apache__druid-14092 | Java | no stop; reached, 5 values | no stop; reached, 5 values | gold solved, noop not solved | **survives** | no stop; read 4 of 4 |
| sphinx-doc__sphinx-7462 | Python | reached, 1 values; reached, 1 values | reached, 1 values; reached, 1 values | gold solved, noop not solved | **survives** | read 1 of 1; read 1 of 1 |
| apache__dubbo-10638 | Java | no stop; reached, 2 values | no stop; reached, 2 values | gold solved, noop not solved | **survives** | no stop; read 2 of 2 |
| matplotlib__matplotlib-21568 | Python | reached, 3 values | reached, 3 values | gold solved, noop not solved | **survives** | read 3 of 3 |
| django__django-14170 | Python | no stop; reached, 4 values | no stop; reached, 4 values | gold solved, noop not solved | **survives** | no stop; read 4 of 4 |
| ponylang__ponyc-4067 | C | refused (UNSUPPORTED) | refused (UNSUPPORTED) | — | dropped | — |
| BurntSushi__ripgrep-1294 | Rust | refused (UNSUPPORTED); refused (UNSUPPORTED) | refused (UNSUPPORTED); refused (UNSUPPORTED) | — | dropped | — |
| astropy__astropy-13033 | Python | reached, 3 values | reached, 3 values | gold solved, noop not solved | **survives** | read 3 of 3 |
| fasterxml__jackson-databind-3851 | Java | reached, 3 values | reached, 3 values | gold solved, noop not solved | **survives** | read 3 of 3 |
| sympy__sympy-21596 | Python | reached, 12 values | reached, 12 values | gold solved, noop not solved | **survives** | refused (INVALID_REQUEST) |
| alibaba__fastjson2-2285 | Java | refused (INVALID_REQUEST) | refused (INVALID_REQUEST) | — | dropped | — |
| clap-rs__clap-2085 | Rust | refused (BUILD_FAILED) | refused (BUILD_FAILED) | — | dropped | — |
| google__gson-1100 | Java | no stop; reached, 2 values | no stop; reached, 2 values | gold solved, noop not solved | reached, after the refill's end | — |
| projectlombok__lombok-3674 | Java | no stop; reached, 5 values | no stop; reached, 5 values | gold solved, noop not solved | reached, after the refill's end | — |
| clap-rs__clap-2882 | Rust | refused (UNSUPPORTED); refused (UNSUPPORTED) | refused (UNSUPPORTED); refused (UNSUPPORTED) | — | dropped | — |
| sympy__sympy-16597 | Python | no stop; no stop | no stop; no stop | — | dropped | — |
| fasterxml__jackson-databind-4048 | Java | refused (INVALID_BREAKPOINT) | refused (INVALID_BREAKPOINT) | — | dropped | — |
| sphinx-doc__sphinx-7590 | Python | no stop; reached, 1 values | no stop; reached, 1 values | gold solved, noop not solved | reached, after the refill's end | — |
| google__gson-1703 | Java | refused (INVALID_REQUEST) | refused (INVALID_REQUEST) | — | dropped | — |

## Counts after each step

On `3e51ccd7`, the first pass and the refill.

| Language | Pool: examined | Step 1 | Step 2 | Reached | Refill: examined | Step 1 | Step 2 | Reached | Counted | Survivors |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| Python | 20 | 16 | 16 | 16 | 14 | 12 | 12 | 10 | 9 | 25 |
| Java | 16 | 16 | 16 | 10 | 14 | 14 | 14 | 7 | 5 | 15 |
| C | 8 | 8 | 8 | 3 | 6 | 5 | 5 | 2 | 2 | 5 |
| C++ | 8 | 8 | 8 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| Rust | 16 | 16 | 16 | 5 | 6 | 5 | 5 | 0 | 0 | 5 |
| **All** | **68** | **64** | **64** | **34** | **40** | **36** | **36** | **19** | **16** | **50** |

- **50 survive:** the pool's 34, and the refill's first 16, which the 27th reserve completes.
  Reserves 28 to 40 were examined at the same time, and 3 of them were reached (gson-1100,
  lombok-3674, sphinx-7590), but they come after the 50th and don't count.
- A refill candidate dropped at step 3 as soon as its failing test couldn't build at base, or its
  fix had no gold point, skipped step 2; it counts under step 2 above.

## The first pass: why candidates were dropped

**Step 1 (4):**

- the failing tests need the network (sphinx-7985);
- the project doesn't run on CPython 3.11 or 3.12, which Peeky needs (sphinx-9229, pytest-5840);
- a failing test already passes at base (django-12406).

**Step 2:** none.

**Step 3 (30), not reached:**

| Why | Count | Candidates |
| --- | --- | --- |
| The failing test can't build at base: it uses what the fix adds | 11 | jackson-databind-2036, -4159; logstash-15964; mockito-3133; json-2576, -1138; zstd-1107; tokio-5781, -6252, -6339; tracing-1045 |
| No gold point: the fix only adds functions, impls or manifest lines | 5 | jackson-databind-4131, simdjson-1615, fmt-3537, tokio-4384, tokio-7124 |
| Peeky refused: a C++ unit with a `<stdin>` line table (#495) | 3 | json-2866, fmt-1390, fmt-3260 |
| Peeky refused: Catch2's `-ffile-prefix-map` leaves no absolute path (#496) | 1 | Catch2-2723 |
| Peeky refused: the fix is Pony code (#497) | 1 | ponyc-3777 |
| Peeky refused: a `[patch]` table in Cargo.toml (#498; 11292's is empty) | 2 | nushell-10629, -11292 |
| Peeky refused: `-p clap` is ambiguous (a dev-dependency brings clap 2.34; #499) | 3 | clap-2161, -2696, -3391 |
| Stopped, but read no value: debug information over Peeky's 300 MiB limit (#483) | 1 | ponyc-3763 |
| Stopped, but read no value: units compiled inside one link command (`-dumpdir`, #500) | 1 | zstd-836 |
| No stop: at base, the failing test fails before the gold lines | 2 | fastjson2-1245, zstd-1530 |

- Most of the first kind are Multi-SWE-bench feature requests: the hidden test calls an API the
  fix adds. Such a failing test can't run before the fix, so neither an agent nor Peeky can watch
  it fail. For the refill, a candidate like this is dropped as soon as its test patch fails to
  build at base.
- Peeky's refusals and the two empty stops are reach gaps, filed as #495 to #500, with a comment
  on #483. None was worked around.

## The refill: why candidates were dropped

The refill examined the 40 reserves of [candidates/refill.txt](candidates/refill.txt). Of those up
to the 27th, where the refill ends:

| Why | Candidates (refill #) |
| --- | --- |
| Step 1: doesn't run on CPython 3.11 or 3.12 | astropy-7606 (7) |
| Step 1: a failing test already passes at base | clap-2642 (18) |
| The failing test can't build at base | zstd-1080 (17), jackson-core-1182 (22) |
| No gold point | jackson-dataformat-xml-590 (10), lombok-2792 (20) |
| No stop: the failing tests fail before the gold lines | jackson-core-1016 (2) |
| Refused: an empty `[patch]` table (#498) | nushell-13870 (6) |
| Refused: a C++ unit with a `<stdin>` line table (#495) | ponyc-4067 (23) |
| Refused: a version 1 Cargo.lock (#503) | ripgrep-1294 (24) |
| Stopped with no locals, and generic code (#502) | tracing-2001 (12) |

After the 27th: sphinx-9461 and zstd-1008 at step 1; django-13794 (no gold point), sympy-16597
(no stop), clap-2085 (#499), clap-2882 (a procedural macro, #504), jackson-databind-4048 (the
point's line, `} else {`, has no code), and fastjson2-2285 and gson-1703 (#501) at step 3.

The refill raised no new ruling. Its agents' choices are in each bug's files:

- A hunk that only adds a nested helper function, or a module-level table entry, was taken as a
  declaration, so astropy-13033, sympy-22080 and sympy-21596 have one gold point each. Each was
  reached there.
- matplotlib-21568 drops 7 image tests from its pass-to-pass list. They fail at base and with the
  fix on CPython 3.11, as the rule for pass-to-pass tests allows.
- dubbo-10638's image installs `libnss-myhostname`, so its test runner can resolve the
  container's own name with no network.

## Rulings after the first pass

The coordinator made these before any agent run. Each is in the README or the bug's files.

- **A gold point runs the first failing test that runs its line.** The brief said "the first
  failing test". django-11400's and django-13195's first failing tests don't run the gold lines,
  but their other failing tests do. Their gold points now name those tests, and both were reached:
  django-11400 read 4 values at each gold point, and django-13195 read 3 and 4. Their first runs'
  notes are in `reach-first-run.txt`.
- **A failing test that can't build at base can't reach anything.** zstd-1107's agent ran the
  fuzzer built from base's `fuzzer.c` instead, and reached a gold point. That isn't the failing
  test, so zstd-1107 is dropped, as the 10 others of its kind were.
- **Java runs on JDK 9 or later.** fastjson2-82 was built on JDK 8, as Multi-SWE-bench builds it.
  Peeky launches Java programs on JDK 9 or later only, so its Peeky image failed the image check.
  It was rebuilt on the base image's JDK 11. Grading gave the same results (base unsolved, gold
  solved twice), and the reach check reached both gold points again.
- **Run corrections that Peeky's message names are allowed.** pytest-10356 (pytest is the project,
  so `--run src/pytest/__main__.py`) and sympy-17318 (`bin/test` copied to a `.py` entry). Their
  first runs' records are kept in `reach-first-run.json`.
- **reach.py** no longer waits out `launch_timeout` when a launch dies: the container's PID 1
  doesn't reap it, and `kill -0` found the zombie. Outcomes are unchanged; only the wait is.

## Adaptations to the environments

Each is in the bug's bug.yaml. None touches the fix or the hidden tests.

- Python: 13 of the 16 survivors run on CPython 3.11, as their pinned dependencies need. astropy-8707
  and -8872 pin pytest 7.1.3, because 7.4 turns their old-style `setup()` warnings into errors.
  astropy-13236 builds with Cython 0.29.30, as 0.29.22's C doesn't compile on 3.11.
- Java: jackson-databind runs on JDK 11, with a stand-in for its parent POM snapshot, which no
  repository serves now. Logstash's `bootstrap` task, which always calls rubygems.org, is skipped
  once its gems are installed.
- C: ponyc builds on shared LLVM images (13.0.1 and 15.0.7), each from a `Dockerfile.llvm` in its
  first bug's directory. Peeky's ponyc builds link the C++ parts with libstdc++, as the prebuilt
  LLVM and gtest require; the fix's lines are all C.
- Rust: nushell's images use its pinned toolchain (1.70.0 and 1.72.1), as Rust 1.96 can't build
  its locked `time` crate. Peeky's builds use 1.96.
- Several Multi-SWE-bench test lists are partial, so some candidates grade fewer pass-to-pass
  tests. Each bug.yaml says how many.

## Survivors

The first pass's and the refill's, and the deciding run's on `1422812c`: the same 50.

| Language | The pool | The refill |
| --- | --- | --- |
| Python (25) | astropy-14598, -8707, -13977, -13236, -8872; matplotlib-23476, -26208, -24870; sympy-17318; pylint-8898; django-11820, -12325, -13195, -11400; pytest-10356; sphinx-10435 | matplotlib-23299, -21568; django-11734, -14170; sympy-22080, -21596; scikit-learn-26194; sphinx-7462; astropy-13033 |
| Java (15) | jackson-databind-4087, -3716, -3666, -3509; lombok-3215; logstash-16579, -16094, -15928; fastjson2-82; dubbo-7041 | mockito-3173; logstash-16681; druid-14092; dubbo-10638; jackson-databind-3851 |
| C (5) | jq-2840; ponyc-4583, -4057 | zstd-2130; ponyc-4017 |
| C++ (0) | | |
| Rust (5) | tokio-5179; ripgrep-2295, -2576; clap-5873, -4005 | |

## Every candidate

### The pool

| Candidate | Language | Tier | Step 1 | Step 2 | Gold points | Result | Note |
| --- | --- | --- | --- | --- | --- | --- | --- |
| astropy__astropy-14598 | Python | 1 | ok | ok | reached, 10 values | **survives** |  |
| django__django-11820 | Python | 1 | ok | ok | reached, 9 values | **survives** |  |
| matplotlib__matplotlib-23476 | Python | 1 | ok | ok | reached, 2 values | **survives** |  |
| sympy__sympy-17318 | Python | 1 | ok | ok | reached, 2 values | **survives** | run corrected as Peeky's message says: `bin/test` copied to a `.py` entry point |
| pylint-dev__pylint-8898 | Python | 1 | ok | ok | reached, 2 values | **survives** |  |
| pytest-dev__pytest-10356 | Python | 1 | ok | ok | reached, 0 values; reached, 1 values | **survives** | run corrected as Peeky's message says: `--run src/pytest/__main__.py` (pytest is the project) |
| sphinx-doc__sphinx-7985 | Python | 1 | fail | — | — | dropped | step 1: its failing tests check external links, so they need the network |
| astropy__astropy-8707 | Python | 1 | ok | ok | reached, 2 values; no stop | **survives** |  |
| django__django-12325 | Python | 1 | ok | ok | reached, 20 values; reached, 3 values | **survives** |  |
| matplotlib__matplotlib-26208 | Python | 1 | ok | ok | reached, 2 values; no stop | **survives** |  |
| sphinx-doc__sphinx-9229 | Python | 1 | fail | — | — | dropped | step 1: doesn't run on CPython 3.11 or 3.12 (imports `types.Union`) |
| astropy__astropy-13977 | Python | 1 | ok | ok | reached, 5 values | **survives** |  |
| django__django-13195 | Python | 1 | ok | ok | reached, 3 values; reached, 4 values | **survives** | reached after the coordinator's fix: each gold point now runs the first failing test that runs it |
| matplotlib__matplotlib-24870 | Python | 1 | ok | ok | reached, 2 values; no stop | **survives** |  |
| sphinx-doc__sphinx-10435 | Python | 1 | ok | ok | reached, 5 values | **survives** |  |
| astropy__astropy-13236 | Python | 1 | ok | ok | reached, 9 values | **survives** |  |
| django__django-12406 | Python | 1 | fail | — | — | dropped | step 1: a failing test already passes at base |
| astropy__astropy-8872 | Python | 1 | ok | ok | reached, 7 values; reached, 8 values | **survives** |  |
| django__django-11400 | Python | 1 | ok | ok | reached, 4 values; reached, 4 values | **survives** | reached after the coordinator's fix: each gold point now runs the first failing test that runs it |
| pytest-dev__pytest-5840 | Python | 1 | fail | — | — | dropped | step 1: doesn't run on CPython 3.10 or later (assertion rewriting) |
| fasterxml__jackson-databind-4087 | Java | 1 | ok | ok | reached, 2 values | **survives** |  |
| alibaba__fastjson2-1245 | Java | 1 | ok | ok | no stop | dropped | no stop: at base the test fails earlier in the same method, before the gold line |
| elastic__logstash-16579 | Java | 1 | ok | ok | reached, 4 values; no stop | **survives** |  |
| fasterxml__jackson-databind-3716 | Java | 1 | ok | ok | reached, 4 values | **survives** |  |
| elastic__logstash-16094 | Java | 1 | ok | ok | not attempted; reached, 4 values | **survives** |  |
| fasterxml__jackson-databind-2036 | Java | 1 | ok | ok | error; error | dropped | the failing test doesn't compile at base (uses what the fix adds) |
| elastic__logstash-15928 | Java | 1 | ok | ok | reached, 5 values | **survives** |  |
| fasterxml__jackson-databind-4159 | Java | 1 | ok | ok | error | dropped | the failing test doesn't compile at base (uses what the fix adds) |
| elastic__logstash-15964 | Java | 1 | ok | ok | error | dropped | the failing test doesn't compile at base |
| fasterxml__jackson-databind-3666 | Java | 1 | ok | ok | no stop; reached, 4 values | **survives** |  |
| fasterxml__jackson-databind-3509 | Java | 1 | ok | ok | reached, 3 values; no stop | **survives** |  |
| alibaba__fastjson2-82 | Java | 1 | ok | ok | reached, 1 values; reached, 1 values | **survives** | rebuilt on JDK 11 by the coordinator: Peeky launches Java on JDK 9 or later only |
| mockito__mockito-3133 | Java | 1 | ok | ok | error; error | dropped | the failing test doesn't compile at base |
| fasterxml__jackson-databind-4131 | Java | 1 | ok | ok | not attempted | dropped | no gold point (the fix only adds methods), and the test doesn't compile at base |
| projectlombok__lombok-3215 | Java | 1 | ok | ok | no stop; reached, 4 values | **survives** |  |
| apache__dubbo-7041 | Java | 1 | ok | ok | reached, 4 values | **survives** |  |
| ponylang__ponyc-3763 | C | 1 | ok | ok | reached, 0 values | dropped | reached with no value read: the test binary's debug information is over Peeky's 300 MiB limit |
| facebook__zstd-836 | C | 1 | ok | ok | reached, 0 values; reached, 0 values | dropped | reached with no value read: units built in one compile-and-link command (`-dumpdir`) |
| jqlang__jq-2840 | C | 1 | ok | ok | reached, 8 values | **survives** |  |
| ponylang__ponyc-4583 | C | 1 | ok | ok | reached, 1 values; reached, 3 values | **survives** |  |
| facebook__zstd-1530 | C | 1 | ok | ok | no stop; no stop | dropped | no stop: the fuzzer fails at the hidden check before the gold lines |
| ponylang__ponyc-3777 | C | 1 | ok | ok | refused (UNSUPPORTED) | dropped | refused: the fix is Pony code, which Peeky doesn't read |
| facebook__zstd-1107 | C | 1 | ok | ok | reached, 2 values; no stop | dropped | the failing test doesn't compile at base (calls what the fix adds); a run of base's fuzzer reached a gold point, but it isn't the failing test |
| ponylang__ponyc-4057 | C | 1 | ok | ok | reached, 18 values | **survives** |  |
| nlohmann__json-2866 | C++ | 1 | ok | ok | refused (UNSUPPORTED); refused (UNSUPPORTED) | dropped | refused: no unit of the test binary is judged (a `<stdin>` line table) |
| simdjson__simdjson-1615 | C++ | 1 | ok | ok | not attempted | dropped | no gold point (the fix only adds functions), and the test doesn't compile at base |
| fmtlib__fmt-1390 | C++ | 1 | ok | ok | refused (INVALID_BREAKPOINT) | dropped | refused: INVALID_BREAKPOINT at the gold line; the test unit has a `<stdin>` line table |
| catchorg__Catch2-2723 | C++ | 1 | ok | ok | refused (UNSUPPORTED); refused (UNSUPPORTED) | dropped | refused: Catch2's `-ffile-prefix-map` leaves no absolute path |
| nlohmann__json-2576 | C++ | 1 | ok | ok | refused (INVALID_REQUEST); refused (INVALID_REQUEST) | dropped | the failing test doesn't compile at base |
| fmtlib__fmt-3260 | C++ | 1 | ok | ok | refused (UNSUPPORTED); refused (UNSUPPORTED) | dropped | refused: a `<stdin>` line table |
| nlohmann__json-1138 | C++ | 1 | ok | ok | refused (INVALID_REQUEST); refused (INVALID_REQUEST) | dropped | the failing test doesn't compile at base |
| fmtlib__fmt-3537 | C++ | 1 | ok | ok | not attempted | dropped | no gold point (the fix only adds a function template) |
| nushell__nushell-10629 | Rust | 1 | ok | ok | refused (UNSUPPORTED) | dropped | refused: UNSUPPORTED, a `[patch]` table in Cargo.toml |
| BurntSushi__ripgrep-2295 | Rust | 1 | ok | ok | reached, 5 values | **survives** |  |
| clap-rs__clap-2161 | Rust | 1 | ok | ok | refused (BUILD_FAILED); refused (BUILD_FAILED) | dropped | refused: BUILD_FAILED, `-p clap` is ambiguous (a dev-dependency brings clap 2.34) |
| tokio-rs__tokio-5781 | Rust | 1 | ok | ok | refused (BUILD_FAILED); refused (BUILD_FAILED) | dropped | the failing test doesn't compile at base (BUILD_FAILED) |
| clap-rs__clap-2696 | Rust | 1 | ok | ok | refused (BUILD_FAILED) | dropped | refused: BUILD_FAILED, `-p clap` is ambiguous (a dev-dependency brings clap 2.34) |
| tokio-rs__tokio-6252 | Rust | 1 | ok | ok | refused (BUILD_FAILED) | dropped | the failing test doesn't compile at base (BUILD_FAILED) |
| clap-rs__clap-3391 | Rust | 1 | ok | ok | refused (BUILD_FAILED); refused (BUILD_FAILED) | dropped | refused: BUILD_FAILED, `-p clap` is ambiguous (a dev-dependency brings clap 2.34) |
| tokio-rs__tokio-6339 | Rust | 1 | ok | ok | refused (BUILD_FAILED) | dropped | the failing test doesn't compile at base (BUILD_FAILED) |
| clap-rs__clap-5873 | Rust | 1 | ok | ok | reached, 7 values | **survives** |  |
| tokio-rs__tokio-5179 | Rust | 1 | ok | ok | reached, 0 values; reached, 1 values | **survives** |  |
| tokio-rs__tokio-4384 | Rust | 1 | ok | ok | not attempted | dropped | no gold point (the fix only adds trait impls) |
| nushell__nushell-11292 | Rust | 1 | ok | ok | refused (UNSUPPORTED); refused (UNSUPPORTED) | dropped | refused: UNSUPPORTED, a `[patch]` table in Cargo.toml |
| tokio-rs__tokio-7124 | Rust | 1 | ok | ok | not attempted | dropped | no gold point (manifests and attributes only) |
| tokio-rs__tracing-1045 | Rust | 1 | ok | ok | refused (BUILD_FAILED); refused (INVALID_REQUEST) | dropped | the failing test doesn't compile at base (BUILD_FAILED) |
| clap-rs__clap-4005 | Rust | 1 | ok | ok | reached, 6 values | **survives** |  |
| BurntSushi__ripgrep-2576 | Rust | 1 | ok | ok | reached, 5 values | **survives** |  |

### The refill, in its order

| # | Candidate | Language | Tier | Step 1 | Step 2 | Gold points | Result | Note |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 1 | matplotlib__matplotlib-23299 | Python | 1 | ok | ok | reached, 2 values | **survives** |  |
| 2 | fasterxml__jackson-core-1016 | Java | 1 | ok | ok | no stop; no stop | dropped |  |
| 3 | django__django-11734 | Python | 1 | ok | ok | reached, 2 values; reached, 6 values | **survives** |  |
| 4 | mockito__mockito-3173 | Java | 1 | ok | ok | reached, 5 values; reached, 2 values | **survives** |  |
| 5 | facebook__zstd-2130 | C | 1 | ok | ok | reached, 4 values; reached, 16 values | **survives** |  |
| 6 | nushell__nushell-13870 | Rust | 1 | ok | ok | refused (UNSUPPORTED); refused (UNSUPPORTED) | dropped |  |
| 7 | astropy__astropy-7606 | Python | 1 | fail | — | — | dropped |  |
| 8 | elastic__logstash-16681 | Java | 2 | ok | ok | reached, 4 values | **survives** |  |
| 9 | sympy__sympy-22080 | Python | 1 | ok | ok | reached, 11 values | **survives** |  |
| 10 | fasterxml__jackson-dataformat-xml-590 | Java | 2 | ok | skipped | — | dropped |  |
| 11 | ponylang__ponyc-4017 | C | 1 | ok | ok | reached, 5 values | **survives** |  |
| 12 | tokio-rs__tracing-2001 | Rust | 1 | ok | ok | reached, 0 values; refused (INVALID_BREAKPOINT) | dropped |  |
| 13 | scikit-learn__scikit-learn-26194 | Python | 1 | ok | ok | reached, 9 values | **survives** |  |
| 14 | apache__druid-14092 | Java | 2 | ok | ok | no stop; reached, 5 values | **survives** |  |
| 15 | sphinx-doc__sphinx-7462 | Python | 1 | ok | ok | reached, 1 values; reached, 1 values | **survives** |  |
| 16 | apache__dubbo-10638 | Java | 2 | ok | ok | no stop; reached, 2 values | **survives** |  |
| 17 | facebook__zstd-1080 | C | 1 | ok | skipped | — | dropped |  |
| 18 | clap-rs__clap-2642 | Rust | 1 | fail | — | — | dropped |  |
| 19 | matplotlib__matplotlib-21568 | Python | 1 | ok | ok | reached, 3 values | **survives** |  |
| 20 | projectlombok__lombok-2792 | Java | 2 | ok | skipped | — | dropped |  |
| 21 | django__django-14170 | Python | 1 | ok | ok | no stop; reached, 4 values | **survives** |  |
| 22 | fasterxml__jackson-core-1182 | Java | 2 | ok | skipped | — | dropped |  |
| 23 | ponylang__ponyc-4067 | C | 1 | ok | ok | refused (UNSUPPORTED) | dropped |  |
| 24 | BurntSushi__ripgrep-1294 | Rust | 1 | ok | ok | refused (UNSUPPORTED); refused (UNSUPPORTED) | dropped |  |
| 25 | astropy__astropy-13033 | Python | 1 | ok | ok | reached, 3 values | **survives** |  |
| 26 | fasterxml__jackson-databind-3851 | Java | 2 | ok | ok | reached, 3 values | **survives** |  |
| 27 | sympy__sympy-21596 | Python | 1 | ok | ok | reached, 12 values | **survives** |  |
| 28 | alibaba__fastjson2-2285 | Java | 2 | ok | ok | refused (INVALID_REQUEST) | dropped |  |
| 29 | facebook__zstd-3362 | C | 1 | ok | skipped | — | dropped |  |
| 30 | clap-rs__clap-2085 | Rust | 1 | ok | ok | refused (BUILD_FAILED) | dropped |  |
| 31 | sphinx-doc__sphinx-9461 | Python | 1 | fail | — | — | dropped |  |
| 32 | google__gson-1100 | Java | 2 | ok | ok | no stop; reached, 2 values | reached, after the refill's end |  |
| 33 | django__django-13794 | Python | 1 | ok | skipped | — | dropped |  |
| 34 | projectlombok__lombok-3674 | Java | 2 | ok | ok | no stop; reached, 5 values | reached, after the refill's end |  |
| 35 | facebook__zstd-1008 | C | 1 | fail | — | — | dropped |  |
| 36 | clap-rs__clap-2882 | Rust | 1 | ok | ok | refused (UNSUPPORTED); refused (UNSUPPORTED) | dropped |  |
| 37 | sympy__sympy-16597 | Python | 1 | ok | ok | no stop; no stop | dropped |  |
| 38 | fasterxml__jackson-databind-4048 | Java | 2 | ok | ok | refused (INVALID_BREAKPOINT) | dropped |  |
| 39 | sphinx-doc__sphinx-7590 | Python | 1 | ok | ok | no stop; reached, 1 values | reached, after the refill's end |  |
| 40 | google__gson-1703 | Java | 2 | ok | ok | refused (INVALID_REQUEST) | dropped |  |
