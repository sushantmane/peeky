# nlohmann/json#2866 at 9426074ce85da2121c315cae8574d8eaca520cb7 (Multi-SWE-bench).
# Build follows Multi-SWE-bench's nlohmann/json recipe (cmake in build/, json_test_data copied
# into build/, ctest), with the base image's GCC 13 for its gcc:12, limited to the test binary of
# the test file the hidden tests change.
FROM peeky-study/base:1
ARG BASE_COMMIT=9426074ce85da2121c315cae8574d8eaca520cb7
RUN git clone -q https://github.com/nlohmann/json.git /testbed && cd /testbed && \
    git checkout -q $BASE_COMMIT && git checkout -q -B main && \
    git remote remove origin && \
    (git tag -l | xargs -r git tag -d > /dev/null) && \
    (git for-each-ref --format='%(refname:short)' refs/heads | grep -vx main | xargs -r git branch -D > /dev/null) && \
    git reflog expire --expire=now --all && git gc --prune=now -q && \
    test "$(git rev-parse HEAD)" = "$BASE_COMMIT" && \
    test "$(git rev-list --all | wc -l)" = "$(git rev-list $BASE_COMMIT | wc -l)" && \
    test -z "$(git fsck --unreachable --no-reflogs 2>/dev/null)"
# The test data the ctest fixture `download_test_data` would clone (cmake/download_test_data.cmake),
# kept outside the build so that grading's clean build finds it with no network.
RUN git clone -q -c advice.detachedHead=false --branch v3.0.0 --depth 1 \
      https://github.com/nlohmann/json_test_data.git /opt/json_test_data
WORKDIR /testbed
RUN cmake -S . -B build > /dev/null && cp -r /opt/json_test_data build/ && \
    cmake --build build --parallel 4 --target test-diagnostics 2>&1 | tail -2
# Warm the test command once at base.
RUN ctest --test-dir build -R '^test-diagnostics$' 2>&1 | tail -4; git status --short | head
