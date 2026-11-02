# fmtlib/fmt#1390 at d6eede9e085f0b36edcf0a2f6dff5f7875181019 (Multi-SWE-bench).
# Build follows Multi-SWE-bench's fmtlib/fmt recipe (cmake in build/, ctest) with the base image's
# GCC 13 for its gcc:latest, limited to the test binary of the test file the hidden tests change.
FROM peeky-study/base:1
ARG BASE_COMMIT=d6eede9e085f0b36edcf0a2f6dff5f7875181019
RUN git clone -q https://github.com/fmtlib/fmt.git /testbed && cd /testbed && \
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
    cmake --build build --parallel 4 --target format-test 2>&1 | tail -2
# Warm the test command once at base.
RUN ctest --test-dir build -R '^format-test$' 2>&1 | tail -4; git status --short | head
