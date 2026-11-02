# nlohmann/json#1138 at e830bc502fe38852654a1f03f963001fecb54a86 (Multi-SWE-bench).
# Build follows Multi-SWE-bench's nlohmann/json recipe (cmake in build/, ctest) with the base
# image's GCC 13 for its gcc:7, limited to the test binary of the test file the hidden tests
# change (ctest runs it twice: test-conversions_default and test-conversions_all). Its Catch
# (1.x) sizes an array with SIGSTKSZ, which glibc 2.34 and later (Ubuntu 24.04 has 2.39) no
# longer define as a constant: the tests compile with CATCH_CONFIG_NO_POSIX_SIGNALS (CXXFLAGS,
# which a fresh CMake configure reads), so Catch doesn't install its own crash handlers.
FROM peeky-study/base:1
ENV CXXFLAGS=-DCATCH_CONFIG_NO_POSIX_SIGNALS
ARG BASE_COMMIT=e830bc502fe38852654a1f03f963001fecb54a86
RUN git clone -q https://github.com/nlohmann/json.git /testbed && cd /testbed && \
    git checkout -q $BASE_COMMIT && git checkout -q -B main && \
    git remote remove origin && \
    (git tag -l | xargs -r git tag -d > /dev/null) && \
    (git for-each-ref --format='%(refname:short)' refs/heads | grep -vx main | xargs -r git branch -D > /dev/null) && \
    git reflog expire --expire=now --all && git gc --prune=now -q && \
    test "$(git rev-parse HEAD)" = "$BASE_COMMIT" && \
    test "$(git rev-list --all | wc -l)" = "$(git rev-list $BASE_COMMIT | wc -l)" && \
    test -z "$(git fsck --unreachable --no-reflogs 2>/dev/null)"
WORKDIR /testbed
RUN cmake -S . -B build > /dev/null && \
    cmake --build build --parallel 4 --target test-conversions 2>&1 | tail -2
# Warm the test command once at base.
RUN ctest --test-dir build -R '^test-conversions_(default|all)$' 2>&1 | tail -4; git status --short | head
