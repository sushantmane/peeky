# facebook/zstd#3362 at 58508398f4121f2a84092ac771db0f2b0fbb3b1a (Multi-SWE-bench).
# Build follows the Multi-SWE-bench recipe (`make`, then the tests' make targets) with the
# base's GCC 13 in place of Ubuntu 20.04's GCC 9. valgrind isn't needed by these targets.
FROM peeky-study/base:1
ARG BASE_COMMIT=58508398f4121f2a84092ac771db0f2b0fbb3b1a
RUN git clone -q https://github.com/facebook/zstd.git /testbed && cd /testbed && \
    git checkout -q $BASE_COMMIT && git checkout -q -B main && \
    git remote remove origin && \
    (git tag -l | xargs -r git tag -d > /dev/null) && \
    (git for-each-ref --format='%(refname:short)' refs/heads | grep -vx main | xargs -r git branch -D > /dev/null) && \
    git reflog expire --expire=now --all && git gc --prune=now -q && \
    test "$(git rev-parse HEAD)" = "$BASE_COMMIT" && \
    test "$(git rev-list --all | wc -l)" = "$(git rev-list $BASE_COMMIT | wc -l)" && \
    test -z "$(git fsck --unreachable --no-reflogs 2>/dev/null)"
WORKDIR /testbed
RUN make -j4 > /dev/null 2>&1 && ./zstd --version
# Warm the test command once at base (its test programs, built and run briefly).
RUN make -C tests test-fuzzer FUZZERTEST=-T10s 2>&1 | tail -5; \
    git status --short | head
