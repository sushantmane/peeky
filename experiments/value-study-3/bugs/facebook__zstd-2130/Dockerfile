# facebook/zstd#2130 at e7d2391e9a75154657183b049b69a7a2effa9724 (Multi-SWE-bench).
# Build follows the Multi-SWE-bench recipe (`make`, then `make test`) with the base's GCC 13 in
# place of Ubuntu 20.04's GCC 9. valgrind isn't needed by these targets.
FROM peeky-study/base:1
ARG BASE_COMMIT=e7d2391e9a75154657183b049b69a7a2effa9724
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
# HAVE_PTHREAD=1: see eval.sh.
RUN make -j4 HAVE_PTHREAD=1 > /dev/null 2>&1 && ./zstd --version
# Warm the test command once at base (`make test`, with eval.sh's times and flags: see eval.sh).
RUN make test HAVE_PTHREAD=1 MOREFLAGS="-g -DDEBUGLEVEL=1 -Werror -Wno-error=strict-aliasing" \
      FUZZERTEST=-T1mn ZSTREAM_TESTTIME=-T30s > /root/warm.log 2>&1; \
    echo "exit $?"; tail -5 /root/warm.log; \
    grep -c "Leaving directory '/testbed/doc/educational_decoder'" /root/warm.log; git status --short | head
