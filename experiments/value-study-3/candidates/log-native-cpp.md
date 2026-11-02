# Round 3 candidates: native-cpp (C++, Multi-SWE-bench)

| instance | control image + validation | Peeky image + gold/noop | reach (gold points: outcome, values read) | result | note |
| --- | --- | --- | --- | --- | --- |
| nlohmann__json-2866 | ok (base not solved; gold-1, gold-2 solved) | ok (gold solved, noop not) | gold include/nlohmann/json.hpp:1310: refused; gold single_include/nlohmann/json.hpp:22873: refused; 0 values (UNSUPPORTED: no unit judged, `<stdin>` from an inheriting constructor) | dropped at step 3: not reached | build 0.03 min (image 1 min), grade 0.03 min; f2p test-diagnostics aborts at base |
| simdjson__simdjson-1615 | ok (base not solved; gold-1, gold-2 solved) | ok (gold solved, noop not; peeky.sh: no benchmarks, -Wno-error for clang 18) | gold: not attempted (the fix only adds new functions; no base line); assertion refused (the new test does not compile at base: no binary); 0 values | dropped at step 3: not reached | build 0.25 min, grade 0.07 min; feature request (at_pointer for On Demand) |
| fmtlib__fmt-1390 | ok (base not solved; gold-1, gold-2 solved) | ok (gold solved, noop not; peeky.sh: -idirafter so test/format does not shadow libc++ <format>) | gold include/fmt/format.h:1732: refused (INVALID_BREAKPOINT: only an unjudged unit has code there); 0 values | dropped at step 3: not reached | build 0.45 min, grade 0.5 min; assertion test/format-test.cc:2630 refused (UNSUPPORTED, `<stdin>` inheriting constructor) |
| catchorg__Catch2-2723 | ok (base not solved; gold-1, gold-2 solved) | ok (gold solved, noop not) | gold catch_run_context.cpp:297 and :675: refused (UNSUPPORTED: no absolute path, the library's own -ffile-prefix-map; without it, `<stdin>` inheriting constructor); 0 values | dropped at step 3: not reached | build 0.16 min, grade 0.36 min; assertion Misc.tests.cpp:222 refused (`<stdin>`) |
| nlohmann__json-2576 | ok (base not solved; gold-1, gold-2 solved) | ok (gold solved, noop not) | gold adl_serializer.hpp:45, single_include json.hpp:20047: refused (INVALID_REQUEST: no binary; the failing test does not compile at base); 0 values | dropped at step 3: not reached | build 0.06 min, grade 0.06 min; doctest 2.4.4 needs DOCTEST_CONFIG_NO_POSIX_SIGNALS with glibc 2.39 (CXXFLAGS) |
| fmtlib__fmt-3260 | ok (base not solved; gold-1, gold-2 solved; with -fsigned-char) | ok (gold solved, noop not) | gold chrono.h:2118 and :2203: refused (UNSUPPORTED: no judged unit has code in chrono.h; chrono-test.cc `<stdin>`); 0 values | dropped at step 3: not reached | build 0.2 min, grade 0.2 min; arm64 unsigned char broke an unrelated case at base and gold: CXXFLAGS=-fsigned-char (judgment call) |
| nlohmann__json-1138 | ok (base not solved; gold-1, gold-2 solved) | ok (gold solved, noop not; peeky.sh: Catch 1.x shuffle defines for libc++ 18) | gold from_json.hpp:286, single_include json.hpp:9784: refused (INVALID_REQUEST: no binary; the failing test does not compile at base); 0 values | dropped at step 3: not reached | build 0.06 min, grade 0.06 min; Catch 1.x needs CATCH_CONFIG_NO_POSIX_SIGNALS with glibc 2.39 (CXXFLAGS) |
| fmtlib__fmt-3537 | ok (base not solved; gold-1, gold-2 solved) | ok (gold solved, noop not) | gold: not attempted (the fix only inserts a new function; no base line); assertion refused (INVALID_REQUEST: no binary; the failing test does not compile at base); 0 values | dropped at step 3: not reached | build 0.1 min, grade 0.1 min |

Counts: 8 examined; after step 1: 8; after step 2: 8; after step 3: 0 survive. All reach records are in
results/reach/3e51ccd7232d/ (Peeky 3e51ccd7232d).

Notes:
- Grading uses Multi-SWE-bench's ctest-level names (its parse_log vendored per repo). Each eval.sh
  builds only the binary of the test file the test patch changes, from a fresh build/, so
  pass_to_pass is limited to the ctest tests eval.sh runs (empty for most; Catch2 keeps its 67
  ctest tests, without the 33 have_flag__* CMake checks; json-2866 keeps download_test_data).
- Two refusal kinds: (a) Peeky judges no unit of fmt, json or Catch2 test binaries built with
  libc++ (`<stdin>` from an inheriting constructor), as in round 2; Catch2's library units also
  carry the project's own -ffile-prefix-map, refused first. (b) Five of the eight fixes are
  compile-time (new overloads or functions): with the test patch, the failing test doesn't compile
  at base, so there is no binary to run; simdjson-1615 and fmt-3537 also have no gold point (the
  fix only inserts new functions).
- Environment adaptations (step 1): json-2576 and json-1138 build their bundled doctest/Catch with
  DOCTEST_CONFIG_NO_POSIX_SIGNALS / CATCH_CONFIG_NO_POSIX_SIGNALS (SIGSTKSZ isn't constant in
  glibc 2.39); fmt-3260 builds with -fsigned-char (an unrelated chrono case assumes x86's signed
  char and failed at base and with gold on arm64); simdjson uses gcc-11 as Multi-SWE-bench does.
- peeky.sh build fixes (step 2): simdjson (no Google Benchmark, -Wno-error appended), fmt-1390
  (-idirafter, as test/format shadows libc++'s <format>), Catch2 (CATCH_ENABLE_WERROR=OFF),
  json-1138 (Catch 1.x shuffle defines for libc++ 18).
- json-2866's assertion point is the binary's first CHECK (line 47): its failing section has no
  assertion of its own (the library's JSON_ASSERT aborts). Later specs use the first assertion of
  the binary's first failing case. Points were fixed before each first run; no run command was
  corrected after a run.
- harness/build_images.sh has no execute bit, so it ran as `harness/heavy.py bash harness/build_images.sh <id>`.
