# catchorg__Catch2-2723: the failing tests' binary (SelfTest) built for Peeky in build-peeky/
# (clang 18, -O0, libc++), beside the project's own build/. The development build's -Werror is
# turned off with the project's own option: clang 18 warns where the project's GCC doesn't.
cmake_build SelfTest -DCATCH_DEVELOPMENT_BUILD=ON -DCATCH_ENABLE_WERROR=OFF
test -x build-peeky/tests/SelfTest
native_note "\`build-peeky/\` is Catch2 and its tests built for Peeky, with libc++ (\`CATCH_DEVELOPMENT_BUILD=ON\`, \`CATCH_ENABLE_WERROR=OFF\`). The project's own build, \`build/\`, is unchanged." \
  "build-peeky/tests/SelfTest (the test runner most of ctest's tests run)" \
  "cd /testbed && cmake --build build-peeky --parallel 4 --target SelfTest" \
  "peeky probe . --binary build-peeky/tests/SelfTest --at tests/SelfTest/<dir>/<name>.tests.cpp:LINE --capture a,b -- \"test case name\""
