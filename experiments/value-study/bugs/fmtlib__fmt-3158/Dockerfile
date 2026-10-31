# fmtlib/fmt#3158 at 80f8d34427d40ec5e7ce3b10ededc46bd4bd5759 (SWE-bench Multilingual).
# Build follows the SWE-bench spec: cmake -B build, target ranges-test.
FROM peeky-study/base:1
ARG BASE_COMMIT=80f8d34427d40ec5e7ce3b10ededc46bd4bd5759
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
    cmake --build build --parallel "$(nproc)" --target ranges-test 2>&1 | tail -2
# Warm the test command once at base.
RUN ctest --test-dir build -V -R ranges-test 2>&1 | tail -4; git status --short | head
