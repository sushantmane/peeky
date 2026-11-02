# Sourced by round 2's docker/prepare.sh in /testbed: ponyc and its runtime (libponyrt) built for
# Peeky in build-peeky/ (CMake's tree in build-peeky/cmake, its outputs in build-peeky/debug, two
# levels below the root as ponyc expects), with the prebuilt libraries in build/libs. The
# project's own build (build/build_debug, build/debug) is unchanged. A Pony program that this
# ponyc compiles links this runtime (build-peeky/debug/libponyrt-pic.a), whose C units Peeky
# reads. libponyrt's CMake looks for the prebuilt llc relative to its build tree; it is named here.
rebuild="cd /testbed && cmake --build build-peeky/cmake --parallel 4 --target ponyc libponyrt"
cmake -S . -B build-peeky/cmake -DCMAKE_BUILD_TYPE=Debug \
  -DCMAKE_C_COMPILER=clang-18 -DCMAKE_CXX_COMPILER=clang++-18 \
  -DCMAKE_C_FLAGS_DEBUG="-O0 $FLAGS -Wno-error" -DCMAKE_CXX_FLAGS_DEBUG="-O0 $FLAGS" \
  -DCMAKE_EXE_LINKER_FLAGS="$LINK" -D_llc_command=/testbed/build/libs/bin/llc >/dev/null
eval "${rebuild#cd /testbed && }" >/dev/null
test -x build-peeky/debug/ponyc && test -f build-peeky/debug/libponyrt-pic.a
exclude /build-peeky/
native_note "\`build-peeky/debug/ponyc\` is ponyc built for Peeky, with its runtime (\`build-peeky/debug/libponyrt-pic.a\`), which every Pony program it compiles links: compile a program with it, then probe the program, whose runtime C units Peeky reads. The project's own build (\`make configure config=debug\`, \`make build config=debug\`: \`build/debug\`) is unchanged." \
  "build-peeky/out/<program>, a program this ponyc compiled (build-peeky/debug/ponyc --debug -o build-peeky/out examples/helloworld writes build-peeky/out/helloworld)" \
  "$rebuild" \
  "peeky probe . --binary build-peeky/out/helloworld --at src/libponyrt/mem/heap.c:LINE --capture a,b" \
  "After rebuilding ponyc or its runtime, compile the program again: it holds the runtime it was linked with."
