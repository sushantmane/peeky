# micropython/micropython#13569 at 2d7fb9a715d8c9f1f7ba73a62cfd587fa6ffc24a (SWE-bench Multilingual).
# Build follows the SWE-bench spec: tools/ci.sh ci_unix_build_helper VARIANT=standard + ffi_lib.so.
FROM peeky-study/base:1
RUN apt-get update -qq && apt-get install -y --no-install-recommends libffi-dev && \
    rm -rf /var/lib/apt/lists/*
ARG BASE_COMMIT=2d7fb9a715d8c9f1f7ba73a62cfd587fa6ffc24a
RUN git clone -q https://github.com/micropython/micropython.git /testbed && cd /testbed && \
    git checkout -q $BASE_COMMIT && git checkout -q -B main && \
    git remote remove origin && \
    (git tag -l | xargs -r git tag -d > /dev/null) && \
    (git for-each-ref --format='%(refname:short)' refs/heads | grep -vx main | xargs -r git branch -D > /dev/null) && \
    git reflog expire --expire=now --all && git gc --prune=now -q && \
    test "$(git rev-parse HEAD)" = "$BASE_COMMIT" && \
    test "$(git rev-list --all | wc -l)" = "$(git rev-list $BASE_COMMIT | wc -l)" && \
    test -z "$(git fsck --unreachable --no-reflogs 2>/dev/null)"
WORKDIR /testbed
# Submodules (network) and the first build.
RUN bash -c 'source ./tools/ci.sh && ci_unix_build_helper VARIANT=standard' && \
    gcc -shared -o tests/ports/unix/ffi_lib.so tests/ports/unix/ffi_lib.c
# Warm the test command once at base.
RUN (cd tests && MICROPY_CPYTHON3=python3 MICROPY_MICROPYTHON=../ports/unix/build-standard/micropython ./run-tests.py -i try) | tail -5; \
    rm -rf tests/results; git status --short | head -20
