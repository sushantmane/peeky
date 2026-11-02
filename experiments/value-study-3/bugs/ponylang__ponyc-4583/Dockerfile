# ponylang/ponyc#4583 at 115143f6bc5a90ff19c3af2bee68f3077fc5f1a3 (Multi-SWE-bench).
# Build follows the Multi-SWE-bench recipe for ponyc (`make libs`, `make configure config=debug`,
# `make build config=debug`, `make test config=debug`), with clang 18 as CC/CXX (their image's
# compiler is clang). `make libs` (LLVM 15.0.7, Google Test, Google Benchmark) is prebuilt in
# peeky-study/r3-ponyc-llvm:15.0.7 (Dockerfile.llvm beside this file), an image built FROM
# peeky-study/base:1; build/libs links to its install tree.
FROM peeky-study/r3-ponyc-llvm:15.0.7
ARG BASE_COMMIT=115143f6bc5a90ff19c3af2bee68f3077fc5f1a3
# ponyc's Makefile takes clang only as `clang` (absent here), else cc: CC and CXX name clang 18.
# Its C flags hold -Werror, and clang 18 may warn where the project's CI clang didn't:
# `make configure` passes CMAKE_FLAGS, which keeps warnings as warnings in debug.
ENV CC=clang-18 CXX=clang++-18 CMAKE_FLAGS="-DCMAKE_C_FLAGS_DEBUG='-g -Wno-error'"
RUN git clone -q https://github.com/ponylang/ponyc.git /testbed && cd /testbed && \
    git checkout -q $BASE_COMMIT && git checkout -q -B main && \
    git remote remove origin && \
    (git tag -l | xargs -r git tag -d > /dev/null) && \
    (git for-each-ref --format='%(refname:short)' refs/heads | grep -vx main | xargs -r git branch -D > /dev/null) && \
    git reflog expire --expire=now --all && git gc --prune=now -q && \
    test "$(git rev-parse HEAD)" = "$BASE_COMMIT" && \
    test "$(git rev-list --all | wc -l)" = "$(git rev-list $BASE_COMMIT | wc -l)" && \
    test -z "$(git fsck --unreachable --no-reflogs 2>/dev/null)"
WORKDIR /testbed
RUN mkdir -p build && ln -s /opt/ponyc-libs build/libs && \
    make configure config=debug > /dev/null && \
    make build config=debug build_flags=-j4 > /root/build.log 2>&1; \
    status=$?; tail -30 /root/build.log; grep -E "error:" /root/build.log | head -30; exit $status
# Warm the test command once at base (failures fine).
RUN make test config=debug build_flags=-j4 > /root/warm.log 2>&1; echo "exit $?"; \
    grep -aE "PASSED|FAILED|Passed:" /root/warm.log | tail -15; git status --short | head
