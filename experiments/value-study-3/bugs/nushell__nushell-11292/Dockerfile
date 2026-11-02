# Bug environment for nushell__nushell-11292 (nushell/nushell @ ecb3b3a364b3).
FROM peeky-study/base:1
ENV CARGO_TERM_COLOR=never
# Clone and keep only history up to the base commit, on a single main branch (one layer,
# so no later objects stay in the image).
RUN git clone -q https://github.com/nushell/nushell.git /testbed && cd /testbed && \
    git checkout -q ecb3b3a364b3332ac9df51dbecd780f1f73e21e8 && git checkout -q -B main && \
    git remote remove origin && \
    git tag -l | xargs -r git tag -d >/dev/null && \
    git for-each-ref --format='%(refname)' refs/heads | grep -vx refs/heads/main | xargs -r -n1 git update-ref -d; \
    git reflog expire --expire=now --all && git gc --prune=now -q && \
    test -z "$(git rev-list --all --not ecb3b3a364b3332ac9df51dbecd780f1f73e21e8)" && test "$(git rev-parse HEAD)" = ecb3b3a364b3332ac9df51dbecd780f1f73e21e8
WORKDIR /testbed
# rust-toolchain.toml pins Rust 1.72.1, which the tests need: with the base image's 1.96, the
# locked `time` 0.3.30 doesn't compile (E0282). rustup installs it; the toolchain file selects it.
RUN rustup toolchain install 1.72.1 --profile minimal && cargo --version
RUN cargo fetch --locked
ENV CARGO_NET_OFFLINE=true
RUN cargo build --locked --bin nu && cargo test --locked --bin nu --no-run
# Warm the test command once at base (failures are fine).
RUN (cargo build --bin nu && cargo test --bin nu --no-fail-fast -- tests::test_help) > /dev/null 2>&1; true
