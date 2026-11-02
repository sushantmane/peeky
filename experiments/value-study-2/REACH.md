# The reach check

Where can Peeky stop in each of the 29 bugs, with no agent? [README.md](README.md#the-reach-check)
gives the rules and [harness/reach.py](harness/reach.py) the procedure. Each bug's points are in
[harness/reach_specs.json](harness/reach_specs.json), and every run's records are in
`results/reach/<Peeky commit>/`.

| Peeky (`main`) | What it adds to the run before | Records |
| --- | --- | --- |
| `791510a6` | | `results/reach/791510a690e4/` |
| `42878bbd` | #468: Rust edition 2018 | `results/reach/42878bbd79f0/` |
| `fef02c78`, Part A's | #463, #469, #473, #476, #477, #480, #484, #485, #486, #487 | `results/reach/fef02c78b57b/` |

The tables below are from `fef02c78`, the `main` that Part A uses. Its records fix each bug's
reach group ([README](README.md#reach-groups-and-the-prs-they-wait-on)). Earlier runs' records
are kept as they were; two of them were taken again after a full run:

- bat's and coreutils' on `791510a6`, once reach.py tried "Build again" refusals a second time;
- druid's on `42878bbd`, once its assertion point moved to the first assertion the test runs.

## Summary

| Language | Bugs | A gold point stopped | ...and values read there | Every gold point refused | The assertion stopped |
| --- | --- | --- | --- | --- | --- |
| Python | 9 | 8 | 8 | 0 | 7 |
| Java | 7 | 7 | 7 | 0 | 6 |
| C | 4 | 4 | 4 | 0 | 1 |
| C++ | 3 | 0 | 0 | 3 | 0 |
| Rust | 6 | 2 | 1 | 4 | 4 |
| **All** | **29** | **21** | **20** | **7** | **18** |

On `42878bbd` the totals were 22, 17, 6 and 16.

- **What the reach work bought.** Round 1 found 13 of the 29 projects out of Peeky's reach: the 7
  C and C++ ones and the 6 Rust ones. Now Peeky reads values at a gold point in 5 of them: jq,
  MicroPython, both valkey bugs, and bat.
- **C:** all 4 read values now. #477 brought valkey's locals in; on `42878bbd`, every one was
  withheld. jq's harness still withholds 3 locals, but for a sound reason: they are declared
  below the stop. #469 removed the other reason, `atexit` code no unit covered.
- **Java:** all 7 stop and read values, the 4 JDK 11 projects among them.
- **Python:** 8 of 9. astropy's failing test fails before it reaches any changed line.
- **Rust:**
  - bat reads values at both gold points. Its first build in a fresh container is refused
    "Build again" (#486's message, as git refreshes its index), and the second builds.
  - coreutils stops at its gold point, `uu_app`, which has no locals, so no value is read. Its
    group is "other", by the pre-registered rule.
  - tokio-4867, tokio-6752 and ripgrep are refused before launch, as #463 decided. Their fix's
    code is generic, and only the crates that use it compile it. Their assertions stop and read
    values.
  - axum is refused with `BUDGET_EXHAUSTED`: more than 300 MiB of debug information (#483, open).
- **C++:** fmt's and json's test units record no MD5s and name `<stdin>`, which clang adds for an
  inheriting constructor. So no unit with the fix's code is judged. That waits on phase 3's
  compiler wrapper.

## Groups for Part A

| Group | Bugs |
| --- | --- |
| reached (20) | Python: django-11087, flask-5014, requests-6028, seaborn-3069, seaborn-3187, sphinx-10614, sphinx-7889, xarray-6599. Java: druid-15402, lucene-12022, lucene-13704, lombok-3009, lombok-3052, lombok-3326, rxjava-7597. C: jq-2650, micropython-13569, valkey-1842, valkey-790. Rust: bat-3108. |
| refused (7) | fmt-3158, fmt-3272, json-4237, axum-1934, ripgrep-2209, tokio-4867, tokio-6752 |
| other (2) | astropy-13398 (the failing test fails first), coreutils-6690 (stops; no locals there) |

## Findings without a PR

- **bat's integration test can't find its binary under Peeky's build.** The test runs `bat`
  through `assert_cmd`, which looks for it in Peeky's run directory and panics: `NotFoundError
  { path: "/root/.peeky/rust-builds/<build>/runs/<run>/bin/bat" }`. So its assertion point is "no
  stop". ripgrep's and coreutils' tests find theirs.
- **axum's budget** is #483, still open.

## Why points weren't reached

- **The failing test fails first:** astropy (all 3 points; `ITRS(location=...)` is a `TypeError`
  at the base commit), xarray's later gold line and its assertion, and rxjava's assertion. Each
  test errors out before the line runs.
- **The test never runs that code:** lucene-12022's and lucene-13704's first gold lines,
  lombok-3009's Eclipse handler, seaborn-3187's `utils.py`, and MicroPython's
  `compile_try_except` branch.
- **Not attempted:** valkey's two tests are Tcl. MicroPython's test is a MicroPython script,
  whose output a Python harness compares.
- **jq's grammar:** a breakpoint in `src/parser.y` is refused, by design. The same action in the
  generated `src/parser.c` stops and reads values.

## Every point (`fef02c78`)

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
| burntsushi__ripgrep-2209 | rust | gold | `crates/printer/src/util.rs:85` | refused: `INVALID_BREAKPOINT` Line 85 has no code to stop at in `grep_printer`'s build: it is in `grep_printer::util::<impl at crates/printer/src/util.rs:41:1: 41:29>::replace_all`, a generic or inlined function whose code only the crates that use it compile, for their own types, and Peeky reads none of theirs. Stop where `grep_ |
| burntsushi__ripgrep-2209 | rust | assertion | `tests/regression.rs:1119` | reached; 5 values, 0 withheld |
| django__django-11087 | python | gold | `django/db/models/deletion.py:138` | reached; 3 values, 0 withheld |
| django__django-11087 | python | gold | `django/db/models/deletion.py:223` | reached; 15 values, 0 withheld |
| django__django-11087 | python | assertion | `tests/delete/tests.py:453` | reached; 3 values, 0 withheld |
| fmtlib__fmt-3158 | cpp | gold | `include/fmt/ranges.h:478` | refused: `UNSUPPORTED` No unit of build-peeky/bin/ranges-test Peeky judges has code in include/fmt/ranges.h. Units Peeky doesn't judge name it in their line tables: test/ranges-test.cc and 1 more: its line table records no MD5s, and names <stdin>, which clang adds for an inheriting constructor: the likely cause is a #line |
| fmtlib__fmt-3158 | cpp | gold | `include/fmt/ranges.h:488` | refused: `UNSUPPORTED` No unit of build-peeky/bin/ranges-test Peeky judges has code in include/fmt/ranges.h. Units Peeky doesn't judge name it in their line tables: test/ranges-test.cc and 1 more: its line table records no MD5s, and names <stdin>, which clang adds for an inheriting constructor: the likely cause is a #line |
| fmtlib__fmt-3158 | cpp | assertion | `test/ranges-test.cc:50` | refused: `UNSUPPORTED` test/ranges-test.cc is a unit Peeky doesn't judge: its line table records no MD5s, and names <stdin>, which clang adds for an inheriting constructor: the likely cause is a #line naming another file, as bison and flex write (bison -l, flex -L), or libc++'s system-header pragma (-D_LIBCPP_HAS_NO_PRAGM |
| fmtlib__fmt-3272 | cpp | gold | `include/fmt/format.h:2550` | refused: `INVALID_BREAKPOINT` Line 2550 of include/fmt/format.h is not among the breakpoint lines of the units Peeky judges (`peeky describe` lists them): it has no code, or only a unit Peeky doesn't judge has code there |
| fmtlib__fmt-3272 | cpp | assertion | `test/xchar-test.cc:436` | refused: `UNSUPPORTED` test/xchar-test.cc is a unit Peeky doesn't judge: its line table records no MD5s, and names <stdin>, which clang adds for an inheriting constructor: the likely cause is a #line naming another file, as bison and flex write (bison -l, flex -L), or libc++'s system-header pragma (-D_LIBCPP_HAS_NO_PRAGMA |
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
| nlohmann__json-4237 | cpp | gold | `include/nlohmann/detail/conversions/to_json.hpp:323` | refused: `UNSUPPORTED` Peeky judges no unit of build-peeky/tests/test-udt_cpp11: tests/src/unit-udt.cpp and 1 more: its line table records no MD5s, and names <stdin>, which clang adds for an inheriting constructor: the likely cause is a #line naming another file, as bison and flex write (bison -l, flex -L), or libc++'s sy |
| nlohmann__json-4237 | cpp | gold | `single_include/nlohmann/json.hpp:5700` | refused: `UNSUPPORTED` Peeky judges no unit of build-peeky/tests/test-udt_cpp11: tests/src/unit-udt.cpp and 1 more: its line table records no MD5s, and names <stdin>, which clang adds for an inheriting constructor: the likely cause is a #line naming another file, as bison and flex write (bison -l, flex -L), or libc++'s sy |
| nlohmann__json-4237 | cpp | assertion | `tests/src/unit-udt.cpp:249` | refused: `UNSUPPORTED` Peeky judges no unit of build-peeky/tests/test-udt_cpp11: tests/src/unit-udt.cpp and 1 more: its line table records no MD5s, and names <stdin>, which clang adds for an inheriting constructor: the likely cause is a #line naming another file, as bison and flex write (bison -l, flex -L), or libc++'s sy |
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
| sharkdp__bat-3108 | rust | gold | `src/bin/bat/app.rs:101` | reached; 3 values, 0 withheld (retried; first: `PROVENANCE_CHANGED`) |
| sharkdp__bat-3108 | rust | gold | `src/bin/bat/clap_app.rs:325` | reached; 2 values, 1 withheld |
| sharkdp__bat-3108 | rust | assertion | `tests/integration_tests.rs:1030` | no stop: the program terminated, exit code 101 |
| sphinx-doc__sphinx-10614 | python | gold | `sphinx/ext/inheritance_diagram.py:415` | reached; 10 values, 0 withheld |
| sphinx-doc__sphinx-10614 | python | assertion | `tests/test_ext_inheritance_diagram.py:224` | reached; 6 values, 0 withheld |
| sphinx-doc__sphinx-7889 | python | gold | `sphinx/ext/autodoc/mock.py:56` | reached; 2 values, 0 withheld |
| sphinx-doc__sphinx-7889 | python | assertion | `tests/test_ext_autodoc_mock.py:38` | reached; 1 values, 0 withheld |
| tokio-rs__axum-1934 | rust | gold | `axum/src/routing/path_router.rs:297` | refused: `BUDGET_EXHAUSTED` Peeky keeps at most 300 MiB of a build's debug information in memory, and this build needs more: probe a smaller program (fewer source files, standard headers or std collections). |
| tokio-rs__axum-1934 | rust | assertion | `axum/src/routing/tests/fallback.rs:242` | refused: `BUDGET_EXHAUSTED` Peeky keeps at most 300 MiB of a build's debug information in memory, and this build needs more: probe a smaller program (fewer source files, standard headers or std collections). |
| tokio-rs__tokio-4867 | rust | gold | `tokio/src/sync/broadcast.rs:540` | refused: `INVALID_BREAKPOINT` Line 540 has no code to stop at in `tokio`'s build: it is in `tokio::sync::broadcast::<impl at tokio/src/sync/broadcast.rs:488:1: 488:18>::send`, a generic or inlined function whose code only the crates that use it compile, for their own types, and Peeky reads none of theirs. Stop where `tokio`'s ow |
| tokio-rs__tokio-4867 | rust | gold | `tokio/src/sync/broadcast.rs:885` | refused: `INVALID_BREAKPOINT` Line 885 has no code to stop at in `tokio`'s build: it is in `tokio::sync::broadcast::<impl at tokio/src/sync/broadcast.rs:708:1: 708:20>::recv_ref`, a generic or inlined function whose code only the crates that use it compile, for their own types, and Peeky reads none of theirs. Stop where `tokio`' |
| tokio-rs__tokio-4867 | rust | assertion | `tokio/tests/sync_broadcast.rs:527` | reached; 2 values, 1 withheld |
| tokio-rs__tokio-6752 | rust | gold | `tokio-util/src/time/delay_queue.rs:769` | refused: `INVALID_BREAKPOINT` Line 769 has no code to stop at in `tokio_util`'s build: it is in `tokio_util::time::delay_queue::<impl at tokio-util/src/time/delay_queue.rs:434:1: 434:22>::remove`, a generic or inlined function whose code only the crates that use it compile, for their own types, and Peeky reads none of theirs. St |
| tokio-rs__tokio-6752 | rust | assertion | `tokio-util/tests/time_delay_queue.rs:888` | reached; 4 values, 0 withheld |
| uutils__coreutils-6690 | rust | gold | `src/uu/cp/src/cp.rs:680` | reached; 0 values, 0 withheld |
| uutils__coreutils-6690 | rust | assertion | `tests/by-util/test_cp.rs:5767` | reached; 0 values, 2 withheld |
| valkey-io__valkey-1842 | c | gold | `src/acl.c:2380` | reached; 10 values, 6 withheld |
| valkey-io__valkey-1842 | c | assertion | `-` | not attempted: the test is Tcl (tests/unit/acl.tcl), which Peeky doesn't debug |
| valkey-io__valkey-790 | c | gold | `src/cluster_legacy.c:5667` | reached; 2 values, 2 withheld |
| valkey-io__valkey-790 | c | gold | `src/cluster_legacy.c:5704` | reached; 3 values, 0 withheld |
| valkey-io__valkey-790 | c | assertion | `-` | not attempted: the test is Tcl (tests/unit/cluster/cluster-shards.tcl), which Peeky doesn't debug |
