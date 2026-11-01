# The reach check

Where can Peeky stop in each of the 29 bugs, with no agent? [README.md](README.md#the-reach-check)
gives the rules and [harness/reach.py](harness/reach.py) the procedure. Each bug's points are in
[harness/reach_specs.json](harness/reach_specs.json), and every run's records are in
`results/reach/<Peeky commit>/`.

| Peeky (`main`) | What it adds to the run before | Records |
| --- | --- | --- |
| `791510a6` | | `results/reach/791510a690e4/` |
| `42878bbd` | #468: Rust edition 2018 | `results/reach/42878bbd79f0/` |
| the `main` Part A uses | the open PRs below | to run |

The tables below are from `42878bbd`, the current `main`. Two records were taken again after
the full run, on the same images:

- bat's and coreutils' on `791510a6`, once reach.py tried "Build again" refusals a second time;
- druid's on `42878bbd`, once its assertion point moved to the first assertion the test runs
  ([README](README.md#the-reach-check)).

## Summary

| Language | Bugs | A gold point stopped | ...and values read there | Every gold point refused | The assertion stopped |
| --- | --- | --- | --- | --- | --- |
| Python | 9 | 8 | 8 | 0 | 7 |
| Java | 7 | 7 | 7 | 0 | 6 |
| C | 4 | 4 | 2 | 0 | 1 |
| C++ | 3 | 0 | 0 | 3 | 0 |
| Rust | 6 | 3 | 0 | 3 | 2 |
| **All** | **29** | **22** | **17** | **6** | **16** |

- **What the reach work bought.** Round 1 found 13 of the 29 projects out of Peeky's reach: the 7
  C and C++ ones and the 6 Rust ones. Now 7 of them stop at a gold point: jq, MicroPython, both
  valkey bugs, tokio twice and ripgrep. In 2, jq and MicroPython, Peeky reads values there.
- **Java:** all 7 stop and read values, the 4 JDK 11 projects among them. Round 1's JDK 11 refusal
  is gone.
- **Python:** 8 of 9 stop. astropy's failing test fails before it reaches any changed line.
- **Where it stops but reads nothing:**
  - valkey (2): every local is withheld. Peeky can't replay the unit's preprocessing, which asks
    whether `<stdatomic.h>` exists (`__has_include`). #477 is that proof.
  - Rust in generic code (3): tokio-6752, tokio-4867 and ripgrep stop, but `session inspect`
    fails with `DEBUGGER_ERROR` "Native stopped line does not match prepared symbols". The code
    is generic, and another crate compiles it. #463 refuses such a breakpoint before launch.
- **Where it is refused:**
  - fmt (2) and json (1): their test units record no MD5s and name `<stdin>`, so no unit with
    the fix's code is judged. json's binary is refused whole.
  - axum: `BUDGET_EXHAUSTED`, more than 300 MiB of debug information.
  - bat, coreutils and ripgrep's test: their Peeky builds fail. The reasons are "Cargo wrote no
    dependency file" for a build script or binary, or "Cargo's unit graph of the build changed
    while Peeky read the workspace". Which one varies from run to run.
  - coreutils' fix: a dependency's build script (`portable-atomic`) reads `CARGO_BUILD_RUSTFLAGS`,
    whose value Peeky can't tell, so the build is refused.
- **Values withheld where Peeky stops:** few outside valkey. jq's harness withholds 3 locals: any
  call may return twice, because `atexit` is code no unit covers. #469 judges such toolchain code.

## Which bugs depend on which open PR

| PR | What it does | Bugs | Expected change |
| --- | --- | --- | --- |
| #463 | Rust workspace worker; the guide's Rust workspace recipe; a breakpoint in generic code another crate compiles is refused before launch | all 6 Rust; tokio-6752, tokio-4867, ripgrep-2209 | The guide stops saying crates with dependencies aren't supported, which matters for Part A's uptake. The three generic stops become refusals. |
| #477 | `__has_include` proven from the build's record | valkey-1842, valkey-790; fmt's `test-main.cc` | valkey's locals are read. |
| #480 | C++ `<::` read as C++11 does | fmt-3158, fmt-3272 (`test-main.cc` only) | None at fmt's gold or assertion points: those units stay unjudged. |
| #469 | code linked from the toolchain's static archives judged by its bytes | jq-2650 (its harness), the other C binaries | jq's 3 withheld locals may be read. |
| #473 | phase 1 acceptance, with changes to binary targets | all 7 C and C++ | Unknown until the rerun. |
| #476 (merged after this run, `3892166b`) | Rust split signatures read token by token | the Rust bugs | None expected. |
| none | | axum-1934, bat-3108, coreutils-6690, ripgrep-2209's test | These need fixes of their own: the budget, the build's dependency files and unit graph, and a build script's environment. |

fmt's gold lines are template code in headers, which only the test units instantiate. They stay
out of reach until phase 3's compiler wrapper, which records each input's checksum.

## Why points weren't reached

- **The failing test fails first:** astropy (all 3 points; `ITRS(location=...)` is a
  `TypeError` at the base commit), xarray's later gold line and its assertion, and rxjava's
  assertion. Each test errors out before the line runs.
- **The test never runs that code:** lucene-12022's and lucene-13704's first gold lines, lombok-3009's
  Eclipse handler, seaborn-3187's `utils.py`, MicroPython's `compile_try_except` branch, and
  tokio-4867's `Sender::send`.
- **Not attempted:** valkey's two tests are Tcl. MicroPython's test is a MicroPython script,
  whose output a Python harness compares.
- **jq's grammar:** a breakpoint in `src/parser.y` is refused, by design. The same action in the
  generated `src/parser.c` stops and reads values.

## Every point (`42878bbd`)

| Bug | Lang. | Point | Where | Outcome |
| --- | --- | --- | --- | --- |
| apache__druid-15402 | java | gold | `processing/src/main/java/org/apache/druid/query/groupby/GroupByQueryQueryToolChest.java:654` | reached; 8 values, 0 withheld |
| apache__druid-15402 | java | assertion | `processing/src/test/java/org/apache/druid/query/groupby/GroupByQueryQueryToolChestTest.java:1018` | reached; 10 values, 0 withheld |
| apache__lucene-12022 | java | gold | `lucene/core/src/java/org/apache/lucene/document/ShapeField.java:379` | no stop: the program terminated, exit code 0 |
| apache__lucene-12022 | java | gold | `lucene/core/src/java/org/apache/lucene/document/ShapeField.java:386` | reached; 1 values, 0 withheld |
| apache__lucene-12022 | java | assertion | `lucene/core/src/test/org/apache/lucene/document/TestLatLonShape.java:974` | reached; 12 values, 0 withheld |
| apache__lucene-13704 | java | gold | `lucene/core/src/java/org/apache/lucene/geo/GeoEncodingUtils.java:366` | no stop: the program terminated, exit code 0 |
| apache__lucene-13704 | java | gold | `lucene/core/src/java/org/apache/lucene/geo/GeoEncodingUtils.java:414` | reached; 4 values, 0 withheld |
| apache__lucene-13704 | java | assertion | `lucene/test-framework/src/java/org/apache/lucene/tests/geo/BaseGeoPointTestCase.java:1789` | reached; 13 values, 0 withheld |
| astropy__astropy-13398 | python | gold | `astropy/coordinates/builtin_frames/intermediate_rotation_transforms.py:150` | no stop: the program terminated, exit code 1 |
| astropy__astropy-13398 | python | gold | `astropy/coordinates/builtin_frames/intermediate_rotation_transforms.py:214` | no stop: the program terminated, exit code 1 |
| astropy__astropy-13398 | python | assertion | `astropy/coordinates/tests/test_intermediate_transformations.py:225` | no stop: the program terminated, exit code 1 |
| burntsushi__ripgrep-2209 | rust | gold | `crates/printer/src/util.rs:85` | reached; inspect failed: `DEBUGGER_ERROR` Native stopped line does not match prepared symbols (after `sed -i '1i version = 3' Cargo.lock`; first: `UNSUPPORTED`) |
| burntsushi__ripgrep-2209 | rust | assertion | `tests/regression.rs:1119` | refused: `INVALID_REQUEST` Cargo wrote no dependency file for `ripgrep`'s rg |
| django__django-11087 | python | gold | `django/db/models/deletion.py:138` | reached; 3 values, 0 withheld |
| django__django-11087 | python | gold | `django/db/models/deletion.py:223` | reached; 15 values, 0 withheld |
| django__django-11087 | python | assertion | `tests/delete/tests.py:453` | reached; 3 values, 0 withheld |
| fmtlib__fmt-3158 | cpp | gold | `include/fmt/ranges.h:478` | refused: `UNSUPPORTED` No unit of build-peeky/bin/ranges-test Peeky judges has code in include/fmt/ranges.h: `peeky describe` lists each unit, judged or not and why |
| fmtlib__fmt-3158 | cpp | gold | `include/fmt/ranges.h:488` | refused: `UNSUPPORTED` No unit of build-peeky/bin/ranges-test Peeky judges has code in include/fmt/ranges.h: `peeky describe` lists each unit, judged or not and why |
| fmtlib__fmt-3158 | cpp | assertion | `test/ranges-test.cc:50` | refused: `UNSUPPORTED` No unit of build-peeky/bin/ranges-test Peeky judges has code in test/ranges-test.cc: `peeky describe` lists each unit, judged or not and why |
| fmtlib__fmt-3272 | cpp | gold | `include/fmt/format.h:2550` | refused: `INVALID_BREAKPOINT` Line 2550 of include/fmt/format.h is not among the breakpoint lines of the units Peeky judges (`peeky describe` lists them): it has no code, or only a unit Peeky doesn't judge has code there |
| fmtlib__fmt-3272 | cpp | assertion | `test/xchar-test.cc:436` | refused: `UNSUPPORTED` No unit of build-peeky/bin/xchar-test Peeky judges has code in test/xchar-test.cc: `peeky describe` lists each unit, judged or not and why |
| jqlang__jq-2650 | c | gold | `src/parser.y:714` | refused: `UNSUPPORTED` No unit of jq Peeky judges has code in src/parser.y: `peeky describe` lists each unit, judged or not and why |
| jqlang__jq-2650 | c | gold | `src/parser.c:3236` | reached; 31 values, 1 withheld |
| jqlang__jq-2650 | c | assertion | `src/jq_test.c:195` | reached; 22 values, 3 withheld |
| micropython__micropython-13569 | c | gold | `py/compile.c:1653` | no stop: the program terminated, exit code 0 |
| micropython__micropython-13569 | c | gold | `py/compile.c:1685` | reached; 6 values, 1 withheld |
| micropython__micropython-13569 | c | assertion | `-` | not attempted: the test is a MicroPython script whose output run-tests.py compares with CPython's; neither is the project's C code |
| mwaskom__seaborn-3069 | python | gold | `seaborn/_core/plot.py:1636` | reached; 6 values, 0 withheld |
| mwaskom__seaborn-3069 | python | gold | `seaborn/_core/plot.py:1647` | reached; 6 values, 0 withheld |
| mwaskom__seaborn-3069 | python | assertion | `tests/_core/test_plot.py:652` | reached; 3 values, 0 withheld |
| mwaskom__seaborn-3187 | python | gold | `seaborn/_core/scales.py:381` | reached; 11 values, 0 withheld |
| mwaskom__seaborn-3187 | python | gold | `seaborn/utils.py:702` | no stop: the program terminated, exit code 1 |
| mwaskom__seaborn-3187 | python | assertion | `tests/_core/test_plot.py:2059` | reached; 5 values, 0 withheld |
| nlohmann__json-4237 | cpp | gold | `include/nlohmann/detail/conversions/to_json.hpp:323` | refused: `UNSUPPORTED` Peeky judges no unit of build-peeky/tests/test-udt_cpp11: /testbed/tests/src/unit-udt.cpp and 1 more: its line table records no MD5s, and names <stdin>, which clang adds for an inheriting constructor: the likely cause is a #line naming another file, as bison and flex write (bison -l, flex -L), or li |
| nlohmann__json-4237 | cpp | gold | `single_include/nlohmann/json.hpp:5700` | refused: `UNSUPPORTED` Peeky judges no unit of build-peeky/tests/test-udt_cpp11: /testbed/tests/src/unit-udt.cpp and 1 more: its line table records no MD5s, and names <stdin>, which clang adds for an inheriting constructor: the likely cause is a #line naming another file, as bison and flex write (bison -l, flex -L), or li |
| nlohmann__json-4237 | cpp | assertion | `tests/src/unit-udt.cpp:249` | refused: `UNSUPPORTED` Peeky judges no unit of build-peeky/tests/test-udt_cpp11: /testbed/tests/src/unit-udt.cpp and 1 more: its line table records no MD5s, and names <stdin>, which clang adds for an inheriting constructor: the likely cause is a #line naming another file, as bison and flex write (bison -l, flex -L), or li |
| pallets__flask-5014 | python | gold | `src/flask/blueprints.py:193` | reached; 11 values, 0 withheld |
| pallets__flask-5014 | python | assertion | `tests/test_blueprints.py:260` | reached; 2 values, 0 withheld |
| projectlombok__lombok-3009 | java | gold | `src/core/lombok/eclipse/handlers/HandleFieldDefaults.java:163` | no stop: the program terminated, exit code 0 |
| projectlombok__lombok-3009 | java | gold | `src/core/lombok/javac/handlers/HandleFieldDefaults.java:143` | reached; 10 values, 0 withheld |
| projectlombok__lombok-3009 | java | assertion | `test/core/src/lombok/AbstractRunTests.java:275` | reached; 9 values, 0 withheld |
| projectlombok__lombok-3052 | java | gold | `src/core/lombok/javac/handlers/HandleBuilder.java:289` | reached; 20 values, 0 withheld |
| projectlombok__lombok-3052 | java | gold | `src/core/lombok/javac/handlers/HandleSuperBuilder.java:211` | reached; 19 values, 0 withheld |
| projectlombok__lombok-3052 | java | assertion | `test/core/src/lombok/AbstractRunTests.java:275` | reached; 9 values, 0 withheld |
| projectlombok__lombok-3326 | java | gold | `src/core/lombok/javac/handlers/HandleUtilityClass.java:131` | reached; 9 values, 0 withheld |
| projectlombok__lombok-3326 | java | assertion | `test/core/src/lombok/AbstractRunTests.java:275` | reached; 9 values, 0 withheld |
| psf__requests-6028 | python | gold | `requests/utils.py:977` | reached; 11 values, 0 withheld |
| psf__requests-6028 | python | assertion | `tests/test_utils.py:615` | reached; 2 values, 0 withheld |
| pydata__xarray-6599 | python | gold | `xarray/core/computation.py:1936` | reached; 1 values, 0 withheld |
| pydata__xarray-6599 | python | gold | `xarray/core/computation.py:1944` | no stop: the program terminated, exit code 1 |
| pydata__xarray-6599 | python | assertion | `xarray/tests/test_computation.py:2036` | no stop: the program terminated, exit code 1 |
| reactivex__rxjava-7597 | java | gold | `src/main/java/io/reactivex/rxjava3/internal/operators/observable/ObservableSwitchMap.java:352` | reached; 2 values, 0 withheld |
| reactivex__rxjava-7597 | java | assertion | `src/test/java/io/reactivex/rxjava3/internal/operators/observable/ObservableSwitchTest.java:1454` | no stop: the program terminated, exit code 0 |
| sharkdp__bat-3108 | rust | gold | `src/bin/bat/app.rs:101` | refused: `INVALID_REQUEST` Cargo wrote no dependency file for `bat`'s build-script-main |
| sharkdp__bat-3108 | rust | gold | `src/bin/bat/clap_app.rs:325` | refused: `PROVENANCE_CHANGED` Provenance: Cargo's unit graph of the build changed while Peeky read the workspace and Cargo ran. Build again (retried; first: `PROVENANCE_CHANGED`) |
| sharkdp__bat-3108 | rust | assertion | `tests/integration_tests.rs:1030` | refused: `PROVENANCE_CHANGED` Provenance: Cargo's unit graph of the build changed while Peeky read the workspace and Cargo ran. Build again (retried; first: `PROVENANCE_CHANGED`) |
| sphinx-doc__sphinx-10614 | python | gold | `sphinx/ext/inheritance_diagram.py:415` | reached; 10 values, 0 withheld |
| sphinx-doc__sphinx-10614 | python | assertion | `tests/test_ext_inheritance_diagram.py:224` | reached; 6 values, 0 withheld |
| sphinx-doc__sphinx-7889 | python | gold | `sphinx/ext/autodoc/mock.py:56` | reached; 2 values, 0 withheld |
| sphinx-doc__sphinx-7889 | python | assertion | `tests/test_ext_autodoc_mock.py:38` | reached; 1 values, 0 withheld |
| tokio-rs__axum-1934 | rust | gold | `axum/src/routing/path_router.rs:297` | refused: `BUDGET_EXHAUSTED` Peeky keeps at most 300 MiB of a build's debug information in memory, and this build needs more: probe a smaller program (fewer source files, standard headers or std collections). |
| tokio-rs__axum-1934 | rust | assertion | `axum/src/routing/tests/fallback.rs:242` | refused: `BUDGET_EXHAUSTED` Peeky keeps at most 300 MiB of a build's debug information in memory, and this build needs more: probe a smaller program (fewer source files, standard headers or std collections). |
| tokio-rs__tokio-4867 | rust | gold | `tokio/src/sync/broadcast.rs:540` | no stop: the program terminated, exit code 101 (after `sed -i '1i version = 3' Cargo.lock`; first: `UNSUPPORTED`) |
| tokio-rs__tokio-4867 | rust | gold | `tokio/src/sync/broadcast.rs:885` | reached; inspect failed: `DEBUGGER_ERROR` Native stopped line does not match prepared symbols |
| tokio-rs__tokio-4867 | rust | assertion | `tokio/tests/sync_broadcast.rs:527` | reached; 2 values, 1 withheld |
| tokio-rs__tokio-6752 | rust | gold | `tokio-util/src/time/delay_queue.rs:769` | reached; inspect failed: `DEBUGGER_ERROR` Native stopped line does not match prepared symbols |
| tokio-rs__tokio-6752 | rust | assertion | `tokio-util/tests/time_delay_queue.rs:888` | reached; 4 values, 0 withheld |
| uutils__coreutils-6690 | rust | gold | `src/uu/cp/src/cp.rs:680` | refused: `UNSUPPORTED` Peeky can't tell the value Cargo gives `portable-atomic`'s build script of CARGO_BUILD_RUSTFLAGS, which it says it reads (`rerun-if-env-changed`), so not whether a later build would differ |
| uutils__coreutils-6690 | rust | assertion | `tests/by-util/test_cp.rs:5767` | refused: `PROVENANCE_CHANGED` Provenance: Cargo's unit graph of the build changed while Peeky read the workspace and Cargo ran. Build again (retried; first: `PROVENANCE_CHANGED`) |
| valkey-io__valkey-1842 | c | gold | `src/acl.c:2380` | reached; 0 values, 16 withheld |
| valkey-io__valkey-1842 | c | assertion | `-` | not attempted: the test is Tcl (tests/unit/acl.tcl), which Peeky doesn't debug |
| valkey-io__valkey-790 | c | gold | `src/cluster_legacy.c:5667` | reached; 0 values, 4 withheld |
| valkey-io__valkey-790 | c | gold | `src/cluster_legacy.c:5704` | reached; 0 values, 3 withheld |
| valkey-io__valkey-790 | c | assertion | `-` | not attempted: the test is Tcl (tests/unit/cluster/cluster-shards.tcl), which Peeky doesn't debug |
