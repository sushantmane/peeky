# Bug environment for tokio-rs__tracing-2001 (tokio-rs/tracing @ 63015eb8d9c5).
FROM peeky-study/base:1
ENV CARGO_TERM_COLOR=never
# Clone and keep only history up to the base commit, on a single main branch (one layer,
# so no later objects stay in the image).
RUN git clone -q https://github.com/tokio-rs/tracing.git /testbed && cd /testbed && \
    git checkout -q 63015eb8d9c5261d9a384e11dc84552f23a5209c && git checkout -q -B main && \
    git remote remove origin && \
    git tag -l | xargs -r git tag -d >/dev/null && \
    git for-each-ref --format='%(refname)' refs/heads | grep -vx refs/heads/main | xargs -r -n1 git update-ref -d; \
    git reflog expire --expire=now --all && git gc --prune=now -q && \
    test -z "$(git rev-list --all --not 63015eb8d9c5261d9a384e11dc84552f23a5209c)" && test "$(git rev-parse HEAD)" = 63015eb8d9c5261d9a384e11dc84552f23a5209c
WORKDIR /testbed
# The repo doesn't commit Cargo.lock and Multi-SWE-bench resolves afresh; this pins that
# resolution (Cargo 1.96, `cargo generate-lockfile`, 2 November 2026; Cargo.lock here; git
# ignores it in /testbed). Cargo writes it in the version 2 format, as the workspace's
# rust-version (1.49) asks.
COPY Cargo.lock /testbed/Cargo.lock
RUN cargo fetch --locked
ENV CARGO_NET_OFFLINE=true
RUN cargo test --locked -p tracing --tests --no-run
# Warm the test command once at base (failures are fine).
RUN (cargo test -p tracing --tests --no-fail-fast) > /dev/null 2>&1; true
