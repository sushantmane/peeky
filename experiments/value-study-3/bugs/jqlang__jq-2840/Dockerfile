# jqlang/jq#2840 at 4cf1408e0bbac8fc714b051fe420921905128efd (Multi-SWE-bench).
# Build follows the Multi-SWE-bench recipe: submodules, autoreconf, configure --with-oniguruma=builtin, make.
FROM peeky-study/base:1
ARG BASE_COMMIT=4cf1408e0bbac8fc714b051fe420921905128efd
RUN git clone -q https://github.com/jqlang/jq.git /testbed && cd /testbed && \
    git checkout -q $BASE_COMMIT && git checkout -q -B main && \
    git remote remove origin && \
    (git tag -l | xargs -r git tag -d > /dev/null) && \
    (git for-each-ref --format='%(refname:short)' refs/heads | grep -vx main | xargs -r git branch -D > /dev/null) && \
    git reflog expire --expire=now --all && git gc --prune=now -q && \
    test "$(git rev-parse HEAD)" = "$BASE_COMMIT" && \
    test "$(git rev-list --all | wc -l)" = "$(git rev-list $BASE_COMMIT | wc -l)" && \
    test -z "$(git fsck --unreachable --no-reflogs 2>/dev/null)"
WORKDIR /testbed
RUN git submodule update --init && autoreconf -fi > /dev/null 2>&1 && \
    ./configure --with-oniguruma=builtin > /dev/null && make clean > /dev/null && \
    touch src/parser.y src/lexer.l && make -j4 > /dev/null 2>&1 && ./jq --version
# Warm the test command once at base.
RUN make check 2>&1 | grep -E "^(PASS|FAIL|# )" | tail -12; git status --short | head
