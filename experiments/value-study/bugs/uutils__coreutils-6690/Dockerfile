# Bug environment for uutils__coreutils-6690 (uutils/coreutils @ af86aee6b86a).
FROM peeky-study/base:1
ENV CARGO_TERM_COLOR=never
# Clone and keep only history up to the base commit, on a single main branch (one layer,
# so no later objects stay in the image).
RUN git clone -q https://github.com/uutils/coreutils.git /testbed && cd /testbed && \
    git checkout -q af86aee6b86a99736ca304ff656d0e4c6d547333 && git checkout -q -B main && \
    git remote remove origin && \
    git tag -l | xargs -r git tag -d >/dev/null && \
    git for-each-ref --format='%(refname)' refs/heads | grep -vx refs/heads/main | xargs -r -n1 git update-ref -d; \
    git reflog expire --expire=now --all && git gc --prune=now -q && \
    test -z "$(git rev-list --all --not af86aee6b86a99736ca304ff656d0e4c6d547333)" && test "$(git rev-parse HEAD)" = af86aee6b86a99736ca304ff656d0e4c6d547333
WORKDIR /testbed
RUN cargo fetch
ENV CARGO_NET_OFFLINE=true
RUN cargo test --no-run
# Warm the test command once at base (failures are fine).
RUN (cargo test --no-fail-fast -- test_cp_cp test_cp_same_file test_cp_multiple_files test_cp_single_file test_cp_no_file) > /dev/null 2>&1; true
