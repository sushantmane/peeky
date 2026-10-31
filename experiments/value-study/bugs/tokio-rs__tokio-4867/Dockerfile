# Bug environment for tokio-rs__tokio-4867 (tokio-rs/tokio @ 53cf021b813c).
FROM peeky-study/base:1
ENV CARGO_TERM_COLOR=never
# Clone and keep only history up to the base commit, on a single main branch (one layer,
# so no later objects stay in the image).
RUN git clone -q https://github.com/tokio-rs/tokio.git /testbed && cd /testbed && \
    git checkout -q 53cf021b813c61cdeace26e863c89f65f6e92abd && git checkout -q -B main && \
    git remote remove origin && \
    git tag -l | xargs -r git tag -d >/dev/null && \
    git for-each-ref --format='%(refname)' refs/heads | grep -vx refs/heads/main | xargs -r -n1 git update-ref -d; \
    git reflog expire --expire=now --all && git gc --prune=now -q && \
    test -z "$(git rev-list --all --not 53cf021b813c61cdeace26e863c89f65f6e92abd)" && test "$(git rev-parse HEAD)" = 53cf021b813c61cdeace26e863c89f65f6e92abd
WORKDIR /testbed
# The repo doesn't commit Cargo.lock; pin dependencies with SWE-bench's lock file
# (swebench/harness/constants/fixtures) so they build with this toolchain.
COPY Cargo.lock /testbed/Cargo.lock
RUN cargo fetch --locked
ENV CARGO_NET_OFFLINE=true
RUN cargo test --locked --test sync_broadcast --no-fail-fast --no-run
# Warm the test command once at base (failures are fine).
RUN (cargo test --test sync_broadcast --no-fail-fast) > /dev/null 2>&1; true
