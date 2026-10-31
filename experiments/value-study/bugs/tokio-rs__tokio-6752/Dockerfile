# Bug environment for tokio-rs__tokio-6752 (tokio-rs/tokio @ ab53bf0c4727).
FROM peeky-study/base:1
ENV CARGO_TERM_COLOR=never
# Clone and keep only history up to the base commit, on a single main branch (one layer,
# so no later objects stay in the image).
RUN git clone -q https://github.com/tokio-rs/tokio.git /testbed && cd /testbed && \
    git checkout -q ab53bf0c4727ae63c6a3a3d772b7bd837d2f49c3 && git checkout -q -B main && \
    git remote remove origin && \
    git tag -l | xargs -r git tag -d >/dev/null && \
    git for-each-ref --format='%(refname)' refs/heads | grep -vx refs/heads/main | xargs -r -n1 git update-ref -d; \
    git reflog expire --expire=now --all && git gc --prune=now -q && \
    test -z "$(git rev-list --all --not ab53bf0c4727ae63c6a3a3d772b7bd837d2f49c3)" && test "$(git rev-parse HEAD)" = ab53bf0c4727ae63c6a3a3d772b7bd837d2f49c3
WORKDIR /testbed
# The repo doesn't commit Cargo.lock; pin dependencies with SWE-bench's lock file
# (swebench/harness/constants/fixtures) so they build with this toolchain.
COPY Cargo.lock /testbed/Cargo.lock
RUN cargo fetch --locked
ENV CARGO_NET_OFFLINE=true
RUN cargo test --locked --test time_delay_queue --no-fail-fast --no-run
# Warm the test command once at base (failures are fine).
RUN (cargo test --test time_delay_queue --no-fail-fast) > /dev/null 2>&1; true
