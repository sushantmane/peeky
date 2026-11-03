# Sourced by round 2's docker/prepare.sh in /testbed: ponyc and its libponyc tests (Google Test)
# built for Peeky in build-peeky/ (CMake's tree in build-peeky/cmake, its outputs in
# build-peeky/debug, two levels below the root as the libponyc tests expect), with the prebuilt
# libraries in build/libs. The project's own build (build/build_debug, build/debug) is unchanged.
# C++ units use libstdc++: the prebuilt LLVM and Google Test that they link with are built with
# it. libponyrt's CMake looks for the prebuilt llc relative to its build tree; it is named here.
rebuild="cd /testbed && cmake --build build-peeky/cmake --parallel 4 --target libponyc.tests ponyc"
cmake -S . -B build-peeky/cmake -DCMAKE_BUILD_TYPE=Debug \
  -DCMAKE_C_COMPILER=clang-18 -DCMAKE_CXX_COMPILER=clang++-18 \
  -DCMAKE_C_FLAGS_DEBUG="-O0 $FLAGS -Wno-error" -DCMAKE_CXX_FLAGS_DEBUG="-O0 $FLAGS" \
  -DCMAKE_EXE_LINKER_FLAGS="$LINK" -D_llc_command=/testbed/build/libs/bin/llc >/dev/null
eval "${rebuild#cd /testbed && }" >/dev/null
test -x build-peeky/debug/libponyc.tests && test -x build-peeky/debug/ponyc
exclude /build-peeky/
native_note "\`build-peeky/\` is ponyc built for Peeky: the compiler and its libponyc tests (Google Test), in \`build-peeky/debug\`. The project's own build (\`make configure config=debug\`, \`make build config=debug\`: \`build/debug\`) is unchanged. C++ units use libstdc++, as the prebuilt LLVM and Google Test in \`build/libs\` do." \
  "build-peeky/debug/libponyc.tests (the compiler: build-peeky/debug/ponyc)" \
  "$rebuild" \
  "peeky probe . --binary build-peeky/debug/libponyc.tests --at src/libponyc/ast/ast.c:LINE --capture a,b -- --gtest_filter='Suite.Name'"
