# simdjson__simdjson-1615: simdjson configured for Peeky in build-peeky/ (clang 18, -O0, libc++),
# beside the project's own build/. The failing test's target comes with the hidden tests, so the
# library is built here, and a test is built there by name.
# - Google Benchmark's configure checks fail with libc++, and the benchmarks and competitors
#   aren't tests: they are left out of this build.
# - Developer mode compiles with -Werror, and clang 18 warns where GCC 11 didn't (e.g.
#   -Wbitwise-instead-of-logical in a header): a -Wno-error is appended after the project's flags,
#   by a CMake file run after project(), so warnings stay warnings.
cat > /opt/peeky/study/no-werror.cmake <<'EOF'
# Peeky's build (build-peeky/): clang 18's newer warnings stay warnings under the project's -Werror.
cmake_language(DEFER CALL target_compile_options simdjson-internal-flags INTERFACE -Wno-error)
EOF
cmake_build simdjson -DSIMDJSON_DEVELOPER_MODE=ON -DSIMDJSON_GOOGLE_BENCHMARKS=OFF \
  -DSIMDJSON_COMPETITION=OFF -DCMAKE_PROJECT_simdjson_INCLUDE=/opt/peeky/study/no-werror.cmake
test -f build-peeky/libsimdjson.a
native_note "\`build-peeky/\` is simdjson and its tests configured for Peeky, with libc++: the library is built, and a test builds there by name. The project's own build, \`build/\`, is unchanged." \
  "build-peeky/tests/<dir>/<test>, once built (tests/dom/, tests/ondemand/ and tests/ hold their sources)" \
  "cd /testbed && cmake -S . -B build-peeky && cmake --build build-peeky --parallel 4 --target <test>" \
  "peeky probe . --binary build-peeky/tests/<dir>/<test> --at tests/<dir>/<test>.cpp:LINE --capture a,b" \
  "Its configure leaves out the benchmarks, and appends \`-Wno-error\` after the project's \`-Werror\` (\`/opt/peeky/study/no-werror.cmake\`): clang 18 warns where the project's GCC didn't. Configure again (the first command above) after adding a test to a CMakeLists.txt."
