# Bug environment for tokio-rs__tracing-1045 (tokio-rs/tracing @ 0dc8ef2da97f).
FROM peeky-study/base:1
ENV CARGO_TERM_COLOR=never
# Clone and keep only history up to the base commit, on a single main branch (one layer,
# so no later objects stay in the image).
RUN git clone -q https://github.com/tokio-rs/tracing.git /testbed && cd /testbed && \
    git checkout -q 0dc8ef2da97f605689fff17f6c38b69e105a5281 && git checkout -q -B main && \
    git remote remove origin && \
    git tag -l | xargs -r git tag -d >/dev/null && \
    git for-each-ref --format='%(refname)' refs/heads | grep -vx refs/heads/main | xargs -r -n1 git update-ref -d; \
    git reflog expire --expire=now --all && git gc --prune=now -q && \
    test -z "$(git rev-list --all --not 0dc8ef2da97f605689fff17f6c38b69e105a5281)" && test "$(git rev-parse HEAD)" = 0dc8ef2da97f605689fff17f6c38b69e105a5281
WORKDIR /testbed
# The repo doesn't commit Cargo.lock and Multi-SWE-bench resolves afresh; this pins that
# resolution (Cargo 1.96, `cargo generate-lockfile`, 2 November 2026; Cargo.lock here; git
# ignores it in /testbed).
COPY Cargo.lock /testbed/Cargo.lock
RUN cargo fetch --locked
ENV CARGO_NET_OFFLINE=true
RUN cargo test --locked -p tracing-core --test dispatch --test global_dispatch --no-fail-fast --no-run
# Warm the test command once at base (failures are fine).
RUN (cargo test -p tracing-core --test dispatch --test global_dispatch --no-fail-fast) > /dev/null 2>&1; true
