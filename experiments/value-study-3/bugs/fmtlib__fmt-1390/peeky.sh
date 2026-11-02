# fmtlib__fmt-1390: the failing test's binary built for Peeky in build-peeky/ (clang 18, -O0,
# libc++), beside the project's own build/.
# libc++'s <ostream> includes <format>, and the tests' system include directory test/ holds a file
# named `format` that would stand in for it. In this build, system include directories are
# searched after the standard library's (-idirafter), by a CMake file run after project().
cat > /opt/peeky/study/libcxx-first.cmake <<'CMAKE'
# Peeky's build (build-peeky/): the tests' system include directories go after libc++'s headers.
set(CMAKE_INCLUDE_SYSTEM_FLAG_CXX "-idirafter ")
CMAKE
cmake_build format-test -DCMAKE_PROJECT_FMT_INCLUDE=/opt/peeky/study/libcxx-first.cmake
test -x build-peeky/bin/format-test
native_note "\`build-peeky/\` is fmt built for Peeky, with libc++. The project's own build, \`build/\`, is unchanged." \
  "build-peeky/bin/format-test (other tests: build them there by name)" \
  "cd /testbed && cmake --build build-peeky --parallel 4 --target format-test" \
  "peeky probe . --binary build-peeky/bin/format-test --at test/format-test.cc:LINE --capture a,b -- --gtest_filter='suite.name'" \
  "Its configure passes the tests' system include directories with \`-idirafter\` (\`/opt/peeky/study/libcxx-first.cmake\`): libc++'s \`<ostream>\` includes \`<format>\`, which \`test/format\` would otherwise stand in for."
