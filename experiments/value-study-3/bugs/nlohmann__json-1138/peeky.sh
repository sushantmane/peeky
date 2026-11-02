# nlohmann__json-1138: the failing tests' binary built for Peeky in build-peeky/ (clang 18, -O0,
# libc++), beside the project's own build/. Besides the image's CXXFLAGS
# (CATCH_CONFIG_NO_POSIX_SIGNALS, for glibc 2.39), the bundled Catch 1.x needs two defines with
# libc++ 18: its random number generator isn't a valid URNG for libc++'s std::shuffle, so it uses
# std::random_shuffle (CATCH_CONFIG_CPP11_NO_SHUFFLE), which libc++ provides in C++17 only on
# request (_LIBCPP_ENABLE_CXX17_REMOVED_RANDOM_SHUFFLE). Both only matter for --order rand.
cmake_build test-conversions \
  -DCMAKE_CXX_FLAGS="$CXXFLAGS -DCATCH_CONFIG_CPP11_NO_SHUFFLE -D_LIBCPP_ENABLE_CXX17_REMOVED_RANDOM_SHUFFLE"
test -x build-peeky/test/test-conversions
native_note "\`build-peeky/\` is the library's tests built for Peeky, with libc++. The project's own build, \`build/\`, is unchanged." \
  "build-peeky/test/test-conversions (other tests: build them there by name)" \
  "cd /testbed && cmake --build build-peeky --parallel 4 --target test-conversions" \
  "peeky probe . --binary build-peeky/test/test-conversions --at test/src/unit-conversions.cpp:LINE --capture a,b" \
  "Its configure adds \`-DCATCH_CONFIG_CPP11_NO_SHUFFLE -D_LIBCPP_ENABLE_CXX17_REMOVED_RANDOM_SHUFFLE\` to the image's CXXFLAGS: the bundled Catch's test shuffling doesn't compile with libc++ 18 otherwise."
