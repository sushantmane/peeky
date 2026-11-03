# Bug environment for clap-rs__clap-2642 (clap-rs/clap @ 8ca62aa18559).
FROM peeky-study/base:1
ENV CARGO_TERM_COLOR=never
# Clone and keep only history up to the base commit, on a single main branch (one layer,
# so no later objects stay in the image).
RUN git clone -q https://github.com/clap-rs/clap.git /testbed && cd /testbed && \
    git checkout -q 8ca62aa18559d8c30056d6c1ebfda2840e0a322c && git checkout -q -B main && \
    git remote remove origin && \
    git tag -l | xargs -r git tag -d >/dev/null && \
    git for-each-ref --format='%(refname)' refs/heads | grep -vx refs/heads/main | xargs -r -n1 git update-ref -d; \
    git reflog expire --expire=now --all && git gc --prune=now -q && \
    test -z "$(git rev-list --all --not 8ca62aa18559d8c30056d6c1ebfda2840e0a322c)" && test "$(git rev-parse HEAD)" = 8ca62aa18559d8c30056d6c1ebfda2840e0a322c
WORKDIR /testbed
# The repo doesn't commit Cargo.lock (git ignores it) and Multi-SWE-bench resolves afresh; this
# pins that resolution (Cargo 1.96, `cargo generate-lockfile`, 2 November 2026; Cargo.lock here).
COPY Cargo.lock /testbed/Cargo.lock
RUN cargo fetch --locked
ENV CARGO_NET_OFFLINE=true
# `--package clap` is ambiguous (criterion's clap 2), so commands name targets; the workspace's
# default members are clap, clap_derive and clap_generate.
RUN RUSTFLAGS=-Awarnings cargo test --locked --tests --no-run
# Warm the test command once at base (failures are fine).
RUN (RUSTFLAGS=-Awarnings cargo test --tests --no-fail-fast) > /dev/null 2>&1; true
