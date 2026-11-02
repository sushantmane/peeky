# nlohmann__json-2576: the failing test's binary built for Peeky in build-peeky/ (clang 18, -O0,
# libc++), beside the project's own build/. The image's CXXFLAGS (DOCTEST_CONFIG_NO_POSIX_SIGNALS,
# for glibc 2.39) apply here too.
cmake_build test-regression2 -DJSON_BuildTests=ON
test -x build-peeky/test/test-regression2
native_note "\`build-peeky/\` is the library's tests built for Peeky, with libc++. The project's own build, \`build/\`, is unchanged." \
  "build-peeky/test/test-regression2 (other tests: build them there by name)" \
  "cd /testbed && cmake --build build-peeky --parallel 4 --target test-regression2" \
  "peeky probe . --binary build-peeky/test/test-regression2 --at test/src/unit-regression2.cpp:LINE --capture a,b"
