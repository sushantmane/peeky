# Bug environment for nushell__nushell-13870 (nushell/nushell @ 6e1e824473e1).
FROM peeky-study/base:1
ENV CARGO_TERM_COLOR=never
# Clone and keep only history up to the base commit, on a single main branch (one layer,
# so no later objects stay in the image).
RUN git clone -q https://github.com/nushell/nushell.git /testbed && cd /testbed && \
    git checkout -q 6e1e824473e15eba246e1c43704c5d88fa237a17 && git checkout -q -B main && \
    git remote remove origin && \
    git tag -l | xargs -r git tag -d >/dev/null && \
    git for-each-ref --format='%(refname)' refs/heads | grep -vx refs/heads/main | xargs -r -n1 git update-ref -d; \
    git reflog expire --expire=now --all && git gc --prune=now -q && \
    test -z "$(git rev-list --all --not 6e1e824473e15eba246e1c43704c5d88fa237a17)" && test "$(git rev-parse HEAD)" = 6e1e824473e15eba246e1c43704c5d88fa237a17
WORKDIR /testbed
# rust-toolchain.toml pins Rust 1.79.0; rustup installs it (minimal profile) and picks it from
# the toolchain file in /testbed, as the first pass's nushell images did with their pins.
RUN rustup toolchain install 1.79.0 --profile minimal && cargo --version
RUN cargo fetch --locked
ENV CARGO_NET_OFFLINE=true
# The integration tests (tests/main.rs) run target/debug/nu, which cargo builds with them.
RUN cargo test --locked --package nu --test main --no-run && test -x target/debug/nu
# Warm the test command once at base (failures are fine).
RUN (cargo test --package nu --test main -- shell::pipeline::commands::external::) > /dev/null 2>&1; true
