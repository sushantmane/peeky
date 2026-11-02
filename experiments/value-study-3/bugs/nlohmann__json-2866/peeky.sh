# nlohmann__json-2866: the failing test's binary built for Peeky in build-peeky/ (clang 18, -O0,
# libc++), beside the project's own build/.
cmake_build test-diagnostics -DJSON_BuildTests=ON
test -x build-peeky/test/test-diagnostics
native_note "\`build-peeky/\` is the library's tests built for Peeky, with libc++. The project's own build, \`build/\`, is unchanged." \
  "build-peeky/test/test-diagnostics (other tests: build them there by name)" \
  "cd /testbed && cmake --build build-peeky --parallel 4 --target test-diagnostics" \
  "peeky probe . --binary build-peeky/test/test-diagnostics --at test/src/unit-diagnostics.cpp:LINE --capture a,b"
