# ponylang/ponyc#3763 at 847c83430e8dfa645ba753a84754004134a9ca60 (Multi-SWE-bench).
# Build follows the Multi-SWE-bench recipe for ponyc (`make libs`, `make configure config=debug`,
# `make build config=debug`, `make test config=debug`), with clang 18 as CC/CXX (their image's
# compiler is clang). `make libs` (LLVM 9.0.1, Google Test, Google Benchmark) is prebuilt in
# peeky-study/r3-ponyc-llvm:9.0.1 (Dockerfile.llvm beside this file), an image built FROM peeky-study/base:1;
# build/libs links to its install tree.
FROM peeky-study/r3-ponyc-llvm:9.0.1
ARG BASE_COMMIT=847c83430e8dfa645ba753a84754004134a9ca60
# ponyc's Makefile takes clang only as `clang` (absent here), else cc: CC and CXX name clang 18.
# Its C flags hold -Werror, and clang 18 warns where 2021's clang didn't (unused-but-set
# variables): `make configure` passes CMAKE_FLAGS, which keeps warnings as warnings in debug.
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
# Linux 6.1 dropped DECnet's <linux/dn.h>, which src/libponyrt/lang/socket.c includes; it only
# uses its DSO_* constants under #ifdef, so an empty header stands in (no tracked file changes).
RUN printf '/* DECnet was removed from the Linux headers; see the Dockerfile. */\n' > /usr/include/linux/dn.h
RUN mkdir -p build && ln -s /opt/ponyc-libs build/libs && \
    make configure config=debug > /dev/null && \
    make build config=debug build_flags=-j4 > /root/build.log 2>&1; \
    status=$?; tail -30 /root/build.log; grep -E "error:" /root/build.log | head -30; exit $status
# Warm the test command once at base (failures fine).
RUN make test config=debug build_flags=-j4 > /root/warm.log 2>&1; echo "exit $?"; \
    grep -E "^\[  (PASSED|FAILED) |FAILED|Passed:" /root/warm.log | tail -15; git status --short | head
