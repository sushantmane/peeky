# valkey-io/valkey#790 at 35a188833335332980a16239ac2efebf241c8a6a (SWE-bench Multilingual).
# Build follows the SWE-bench spec: make distclean && make (tests use tclsh from the base image).
FROM peeky-study/base:1
ARG BASE_COMMIT=35a188833335332980a16239ac2efebf241c8a6a
RUN git clone -q https://github.com/valkey-io/valkey.git /testbed && cd /testbed && \
    git checkout -q $BASE_COMMIT && git checkout -q -B main && \
    git remote remove origin && \
    (git tag -l | xargs -r git tag -d > /dev/null) && \
    (git for-each-ref --format='%(refname:short)' refs/heads | grep -vx main | xargs -r git branch -D > /dev/null) && \
    git reflog expire --expire=now --all && git gc --prune=now -q && \
    test "$(git rev-parse HEAD)" = "$BASE_COMMIT" && \
    test "$(git rev-list --all | wc -l)" = "$(git rev-list $BASE_COMMIT | wc -l)" && \
    test -z "$(git fsck --unreachable --no-reflogs 2>/dev/null)"
WORKDIR /testbed
RUN make distclean > /dev/null 2>&1; make -j"$(nproc)" > /dev/null 2>&1 && ls -la src/valkey-server
# Warm the test command once at base.
RUN TERM=dumb ./runtest --durable --single unit/cluster/cluster-shards 2>&1 | tail -8; \
    rm -rf tests/tmp/*; git status --short | head
