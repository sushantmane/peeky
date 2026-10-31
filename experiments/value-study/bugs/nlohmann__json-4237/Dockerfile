# nlohmann/json#4237 at 3780b41dd070436f3f55327b0a88f27a52e2dfa8 (SWE-bench Multilingual).
# Build follows the SWE-bench spec: cmake in build/, target test-udt_cpp11.
FROM peeky-study/base:1
ARG BASE_COMMIT=3780b41dd070436f3f55327b0a88f27a52e2dfa8
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
# Configure and build the test target (fetches json_test_data while the network is on).
RUN mkdir -p build && cd build && cmake .. > /dev/null && make test-udt_cpp11 2>&1 | tail -3
# Warm the test command once at base.
RUN ./build/tests/test-udt_cpp11 -s -r=xml | tail -3; git status --short | head
