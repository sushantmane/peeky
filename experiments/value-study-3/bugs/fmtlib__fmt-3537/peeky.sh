# fmtlib__fmt-3537: the failing test's binary built for Peeky in build-peeky/ (clang 18, -O0,
# libc++), beside the project's own build/.
cmake_build color-test
test -x build-peeky/bin/color-test
native_note "\`build-peeky/\` is fmt built for Peeky, with libc++. The project's own build, \`build/\`, is unchanged." \
  "build-peeky/bin/color-test (other tests: build them there by name)" \
  "cd /testbed && cmake --build build-peeky --parallel 4 --target color-test" \
  "peeky probe . --binary build-peeky/bin/color-test --at test/color-test.cc:LINE --capture a,b -- --gtest_filter='suite.name'"
