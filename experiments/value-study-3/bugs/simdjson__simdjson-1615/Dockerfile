# simdjson/simdjson#1615 at 40cba172ed66584cf670c98202ed474a316667e3 (Multi-SWE-bench).
# Build follows Multi-SWE-bench's simdjson recipe (cmake -DSIMDJSON_DEVELOPER_MODE=ON in build/,
# ctest) with GCC 11 as its gcc:11 image has it, limited to the test binary the hidden tests add.
# The dependencies that configure downloads go to /opt/simdjson-deps (simdjson_DEPENDENCY_CACHE_DIR),
# outside the tree, so that later configures need no network.
FROM peeky-study/base:1
ARG BASE_COMMIT=40cba172ed66584cf670c98202ed474a316667e3
RUN apt-get update -qq && apt-get install -y -qq --no-install-recommends gcc-11 g++-11 > /dev/null && \
    rm -rf /var/lib/apt/lists/*
ENV CC=gcc-11 CXX=g++-11 simdjson_DEPENDENCY_CACHE_DIR=/opt/simdjson-deps
RUN git clone -q https://github.com/simdjson/simdjson.git /testbed && cd /testbed && \
    git checkout -q $BASE_COMMIT && git checkout -q -B main && \
    git remote remove origin && \
    (git tag -l | xargs -r git tag -d > /dev/null) && \
    (git for-each-ref --format='%(refname:short)' refs/heads | grep -vx main | xargs -r git branch -D > /dev/null) && \
    git reflog expire --expire=now --all && git gc --prune=now -q && \
    test "$(git rev-parse HEAD)" = "$BASE_COMMIT" && \
    test "$(git rev-list --all | wc -l)" = "$(git rev-list $BASE_COMMIT | wc -l)" && \
    test -z "$(git fsck --unreachable --no-reflogs 2>/dev/null)"
WORKDIR /testbed
RUN mkdir -p /opt/simdjson-deps && cmake -S . -B build -DSIMDJSON_DEVELOPER_MODE=ON > /dev/null && \
    cmake --build build --parallel 4 --target simdjson 2>&1 | tail -2
# Warm the test command once at base (its target doesn't exist yet: failures are fine).
RUN (cmake --build build --parallel 4 --target ondemand_json_pointer_tests; \
     ctest --test-dir build -R '^ondemand_json_pointer_tests$') 2>&1 | tail -4; git status --short | head
