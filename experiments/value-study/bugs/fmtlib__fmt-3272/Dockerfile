# fmtlib/fmt#3272 at 0f42c17d85f26bbbd6a30d71ce8306d371426bfc (SWE-bench Multilingual).
# Build follows the SWE-bench spec: cmake -B build, target xchar-test.
FROM peeky-study/base:1
ARG BASE_COMMIT=0f42c17d85f26bbbd6a30d71ce8306d371426bfc
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
RUN mkdir -p build && cmake -B build -S . > /dev/null && \
    cmake --build build --parallel "$(nproc)" --target xchar-test 2>&1 | tail -2
# Warm the test command once at base.
RUN ctest --test-dir build -V -R xchar-test 2>&1 | tail -4; git status --short | head
