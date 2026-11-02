# catchorg/Catch2#2723 at 4acc51828f7f93f3b2058a63f54d112af4034503 (Multi-SWE-bench).
# Build follows Multi-SWE-bench's catchorg/Catch2 recipe (cmake -DCATCH_DEVELOPMENT_BUILD=ON in
# build/, make, ctest) with the base image's GCC 13 and Python 3.12 for its gcc:latest. build/
# is listed in .git/info/exclude, as the project's .gitignore doesn't name it.
FROM peeky-study/base:1
ARG BASE_COMMIT=4acc51828f7f93f3b2058a63f54d112af4034503
RUN git clone -q https://github.com/catchorg/Catch2.git /testbed && cd /testbed && \
    git checkout -q $BASE_COMMIT && git checkout -q -B main && \
    git remote remove origin && \
    (git tag -l | xargs -r git tag -d > /dev/null) && \
    (git for-each-ref --format='%(refname:short)' refs/heads | grep -vx main | xargs -r git branch -D > /dev/null) && \
    git reflog expire --expire=now --all && git gc --prune=now -q && \
    test "$(git rev-parse HEAD)" = "$BASE_COMMIT" && \
    test "$(git rev-list --all | wc -l)" = "$(git rev-list $BASE_COMMIT | wc -l)" && \
    test -z "$(git fsck --unreachable --no-reflogs 2>/dev/null)" && \
    echo /build/ >> .git/info/exclude
WORKDIR /testbed
RUN cmake -S . -B build -DCATCH_DEVELOPMENT_BUILD=ON > /dev/null && \
    cmake --build build --parallel 4 2>&1 | tail -2
# Warm the test command once at base.
RUN ctest --test-dir build 2>&1 | tail -6; git status --short | head
