# Bug environment for tokio-rs__axum-1934 (tokio-rs/axum @ e97462d45251).
FROM peeky-study/base:1
ENV CARGO_TERM_COLOR=never
# Clone and keep only history up to the base commit, on a single main branch (one layer,
# so no later objects stay in the image).
RUN git clone -q https://github.com/tokio-rs/axum.git /testbed && cd /testbed && \
    git checkout -q e97462d45251c21ffd1ba4fb5db30f3cf6f6cb6c && git checkout -q -B main && \
    git remote remove origin && \
    git tag -l | xargs -r git tag -d >/dev/null && \
    git for-each-ref --format='%(refname)' refs/heads | grep -vx refs/heads/main | xargs -r -n1 git update-ref -d; \
    git reflog expire --expire=now --all && git gc --prune=now -q && \
    test -z "$(git rev-list --all --not e97462d45251c21ffd1ba4fb5db30f3cf6f6cb6c)" && test "$(git rev-parse HEAD)" = e97462d45251c21ffd1ba4fb5db30f3cf6f6cb6c
WORKDIR /testbed
# The repo doesn't commit Cargo.lock; pin dependencies with SWE-bench's lock file
# (swebench/harness/constants/fixtures) so they build with this toolchain.
COPY Cargo.lock /testbed/Cargo.lock
RUN cargo fetch --locked
ENV CARGO_NET_OFFLINE=true
RUN RUSTFLAGS=-Awarnings cargo test --locked --package axum --lib --no-run
# Warm the test command once at base (failures are fine).
RUN (RUSTFLAGS=-Awarnings cargo test --package axum --lib -- routing::tests::fallback) > /dev/null 2>&1; true
