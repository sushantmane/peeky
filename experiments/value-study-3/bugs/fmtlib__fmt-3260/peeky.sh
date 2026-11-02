# fmtlib__fmt-3260: the failing test's binary built for Peeky in build-peeky/ (clang 18, -O0,
# libc++), beside the project's own build/. The image's CXXFLAGS (-fsigned-char, as on x86) apply
# here too.
cmake_build chrono-test
test -x build-peeky/bin/chrono-test
native_note "\`build-peeky/\` is fmt built for Peeky, with libc++. The project's own build, \`build/\`, is unchanged." \
  "build-peeky/bin/chrono-test (other tests: build them there by name)" \
  "cd /testbed && cmake --build build-peeky --parallel 4 --target chrono-test" \
  "peeky probe . --binary build-peeky/bin/chrono-test --at test/chrono-test.cc:LINE --capture a,b -- --gtest_filter='suite.name'"
