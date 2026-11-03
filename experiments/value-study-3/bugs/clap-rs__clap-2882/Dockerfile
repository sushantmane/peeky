# Bug environment for clap-rs__clap-2882 (clap-rs/clap @ f9208ae4e3c2).
FROM peeky-study/base:1
ENV CARGO_TERM_COLOR=never
# Clone and keep only history up to the base commit, on a single main branch (one layer,
# so no later objects stay in the image).
RUN git clone -q https://github.com/clap-rs/clap.git /testbed && cd /testbed && \
    git checkout -q f9208ae4e3c2e1f7eac6de20456f42f45b9d3b8d && git checkout -q -B main && \
    git remote remove origin && \
    git tag -l | xargs -r git tag -d >/dev/null && \
    git for-each-ref --format='%(refname)' refs/heads | grep -vx refs/heads/main | xargs -r -n1 git update-ref -d; \
    git reflog expire --expire=now --all && git gc --prune=now -q && \
    test -z "$(git rev-list --all --not f9208ae4e3c2e1f7eac6de20456f42f45b9d3b8d)" && test "$(git rev-parse HEAD)" = f9208ae4e3c2e1f7eac6de20456f42f45b9d3b8d
WORKDIR /testbed
# The repo doesn't commit Cargo.lock (git ignores it) and Multi-SWE-bench resolves afresh; this
# pins that resolution (Cargo 1.96, `cargo generate-lockfile`, 2 November 2026; Cargo.lock here).
COPY Cargo.lock /testbed/Cargo.lock
RUN cargo fetch --locked
ENV CARGO_NET_OFFLINE=true
# `--package clap` is ambiguous (criterion's clap 2), so commands name targets; the workspace's
# default members are clap, clap_derive, clap_generate and clap_generate_fig.
RUN RUSTFLAGS=-Awarnings cargo test --locked --tests --no-run
# Warm the test command once at base (failures are fine: the hidden test file is new).
RUN (RUSTFLAGS=-Awarnings cargo test -p clap_derive --test help) > /dev/null 2>&1; true
